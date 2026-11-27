import base64
import copy
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import twenty_ai_providers as providers


CONFIG = {
    "litellm": {"baseUrl": "https://lite.example.com/v1", "apiKey": "test-lite-key", "models": ["lite/a", "lite/b"]},
    "cliproxy": {"baseUrl": "https://cli.example.com:8443/v1", "apiKey": "test-cli-key", "models": ["cli-a"]},
}
ENV = {
    f"{name.upper()}_{suffix}": value
    for name, settings in CONFIG.items()
    for suffix, value in (
        ("BASE_URL", settings["baseUrl"]),
        ("API_KEY", settings["apiKey"]),
        ("MODELS_JSON", json.dumps(settings["models"])),
    )
}
HOST = "receiver.example.com"
PUBLIC_KEY = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32).decode("ascii")
PIN = f"{HOST} ssh-ed25519 {PUBLIC_KEY}"


class PayloadTest(unittest.TestCase):
    def test_environment_and_receiver_agree_on_the_configuration(self):
        payload = providers.payload_from_environment(ENV)
        self.assertEqual(json.loads(payload), CONFIG)
        self.assertEqual(providers.payload_from_stream(io.BytesIO(payload)), payload)

    def test_rejects_unsafe_or_malformed_values(self):
        invalid = (
            ("baseUrl", "http://lite.example.com/v1"),
            ("baseUrl", "https://name:password@lite.example.com/v1"),
            ("baseUrl", "https://lite.example.com/v1?"),
            ("baseUrl", "https://lite.example.com/v1#"),
            ("baseUrl", "https://lite.example.com:/v1"),
            ("baseUrl", "https://lite.example.com:0/v1"),
            ("baseUrl", "https://lite.example.com:65536/v1"),
            ("baseUrl", "https://lite.example.com/\npath"),
            ("apiKey", ""),
            ("apiKey", "invalid key"),
            ("apiKey", "key\x00invalid"),
            ("models", "not-an-array"),
            ("models", []),
            ("models", ["id", "id"]),
            ("models", [None]),
            ("models", ["model with space"]),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                invalid_config = copy.deepcopy(CONFIG)
                invalid_config["litellm"][field] = value
                with self.assertRaises(providers.ConfigurationError):
                    providers.checked_payload(invalid_config)

    def test_rejects_unknown_fields_duplicates_and_oversize_input(self):
        with self.assertRaises(providers.ConfigurationError):
            providers.checked_payload({**CONFIG, "extra": {}})
        for raw in (b'{"litellm":{},"litellm":{}}', b"\xff", b"x" * (providers.MAX_PAYLOAD_BYTES + 1)):
            with self.assertRaises(providers.ConfigurationError):
                providers.payload_from_stream(io.BytesIO(raw))

    def test_receiver_rejects_invalid_input_before_writing(self):
        with (
            patch.object(providers.os, "geteuid", return_value=0),
            patch.object(providers.signal, "signal"),
            patch.object(providers.signal, "alarm"),
            patch.object(providers.sys, "stdin", buffer=io.BytesIO(b"incomplete")),
            patch.object(providers, "write_configuration") as write,
        ):
            with self.assertRaises(providers.ConfigurationError):
                providers.receive("teste")
            write.assert_not_called()


class ReceiverTest(unittest.TestCase):
    def test_replacement_sets_permissions_and_keeps_existing_readers_on_old_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o750)
            target = directory / "providers.json"
            before = providers.checked_payload(CONFIG)
            providers.write_configuration(directory, before)
            rotated_config = copy.deepcopy(CONFIG)
            rotated_config["litellm"]["apiKey"] = "test-rotated-key"
            after = providers.checked_payload(rotated_config)
            with target.open("rb") as reader:
                providers.write_configuration(directory, after)
                self.assertEqual(reader.read(), before)
            self.assertEqual(json.loads(target.read_bytes()), rotated_config)
            metadata = target.stat()
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o640)
            self.assertEqual(metadata.st_gid, directory.stat().st_gid)
            self.assertEqual(metadata.st_uid, os.geteuid())
            self.assertEqual(list(directory.iterdir()), [target])

    def test_rejects_an_unprotected_or_symlink_receiver_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaises(providers.ConfigurationError):
                providers.write_configuration(directory, b"example")
            alias = directory / "alias"
            alias.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(OSError):
                providers.write_configuration(alias, b"example")

    def test_failed_replace_keeps_previous_file_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o750)
            target = directory / "providers.json"
            providers.write_configuration(directory, b"previous")
            with patch.object(providers.os, "replace", side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    providers.write_configuration(directory, b"new")
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(directory.iterdir()), [target])


class TransportTest(unittest.TestCase):
    def test_pin_requires_exact_host_and_ed25519_key(self):
        self.assertEqual(providers.checked_pins(HOST, PIN), PIN + "\n")
        for host, line in ((HOST, "* " + PIN.split(" ", 1)[1]), ("other.example.com", PIN), (HOST, PIN + " comment")):
            with self.subTest(line=line), self.assertRaises(providers.ConfigurationError):
                providers.checked_pins(host, line)

    def test_ssh_sends_payload_only_in_stdin_with_fixed_identity_and_strict_pin(self):
        key_paths = []

        def run(argv, **options):
            self.assertEqual(argv[-4:], ["-l", "twenty-ai-config-homologacao", "--", HOST])
            self.assertIn("-oStrictHostKeyChecking=yes", argv)
            self.assertIn("-oClearAllForwardings=yes", argv)
            self.assertNotIn("fake-ssh-private-key", " ".join(argv))
            for provider in CONFIG.values():
                self.assertNotIn(provider["apiKey"], " ".join(argv))
                self.assertNotIn(provider["apiKey"], options["env"].values())
            self.assertEqual(json.loads(options["input"]), CONFIG)
            self.assertEqual(options["stdout"], subprocess.DEVNULL)
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            key = Path(argv[argv.index("-i") + 1])
            key_paths.append(key)
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual(key.read_text(), "fake-ssh-private-key\n")
            pin_arg = next(arg for arg in argv if arg.startswith("-oUserKnownHostsFile="))
            self.assertEqual(Path(pin_arg.split("=", 1)[1]).read_text(), PIN + "\n")
            return subprocess.CompletedProcess(argv, 0)

        env = {
            **ENV,
            "TWENTY_CONFIG_SSH_HOST": HOST,
            "TWENTY_CONFIG_SSH_KNOWN_HOSTS": PIN,
            "TWENTY_CONFIG_SSH_KEY": "fake-ssh-private-key",
        }
        with patch.object(providers.subprocess, "run", side_effect=run) as ssh:
            providers.send("homologacao", env)
            ssh.assert_called_once()
        self.assertFalse(key_paths[0].exists())
