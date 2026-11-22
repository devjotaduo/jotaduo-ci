"""Exercise the private cache against an isolated Git checkout using real tar/zstd."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prepared_cache", Path(__file__).with_name("prepared-cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class PreparedCacheTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        workspace = root / "checkout"
        workspace.mkdir()
        os.chdir(workspace)
        self.output = root / "output"
        env = {k: "fixture" for k in ("GITHUB_REPOSITORY_ID", "GITHUB_WORKFLOW_SHA",
               "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_NAME", "CACHE_NONCE")}
        self.enterContext(patch.dict(os.environ, {**env, "TMPDIR": str(root), "GITHUB_OUTPUT": str(self.output)}))
        slot = root / "cache"
        self.enterContext(patch.multiple(cache, ROOT=slot, ARCHIVE=slot/"prepared.tar.zst",
                          META=slot/"prepared.json", SLOT=slot/"slot", PART=slot/"creating.tar.zst", BYPASS=slot/"bypass",
                          CAP=2**20, RESERVE=2**20))
        self.git("init", "-q")
        self.write(".yarn/releases/yarn.cjs", "release")
        self.write("packages/twenty-shared/src/index.ts", "before")
        self.write("packages/twenty-shared/src/obsolete.ts", "obsolete")
        self.write("packages/app/package.json", "{}")
        self.git("add", ".")
        self.git("-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "base")
        self.command("begin")

    def git(self, *args):
        subprocess.run(["git", *args], check=True)

    def write(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def prepared(self):
        self.write("node_modules/.yarn-state.yml", "links")
        self.write(".yarn/install-state.gz", "state")
        self.write("packages/app/node_modules/sub/index.js", "nested")
        Path("packages/app/node_modules/sub/index.js").chmod(0o755)
        self.write("packages/twenty-shared/src/index.ts", "generated")
        Path("packages/twenty-shared/src/obsolete.ts").unlink()
        self.write("packages/twenty-shared/sub/package.json", "{}")
        self.write("packages/twenty-shared/sub/dist/index.js", "build")
        self.write(".nx/workspace-data/local", "ephemeral")
        Path("node_modules/workspace").symlink_to("../packages/app")

    def command(self, cmd):
        previous = os.umask(0o022)
        try:
            with patch.object(sys, "argv", ["cache", cmd]):
                cache.main()
        finally:
            os.umask(previous)

    def clean(self):
        self.git("reset", "--hard", "-q", "HEAD")
        self.git("clean", "-ffdxq")

    def test_reuse_and_rotation(self):
        self.prepared()
        self.command("save")
        self.clean()
        self.command("restore")
        self.assertIn("hit=true", self.output.read_text())
        self.assertEqual(Path("packages/twenty-shared/src/index.ts").read_text(), "generated")
        self.assertFalse(Path("packages/twenty-shared/src/obsolete.ts").exists())
        self.assertEqual(Path("packages/twenty-shared/sub/dist/index.js").read_text(), "build")
        self.assertEqual(Path("node_modules/workspace/node_modules/sub/index.js").read_text(), "nested")
        self.assertEqual(stat.S_IMODE(Path("node_modules/workspace/node_modules/sub/index.js").stat().st_mode), 0o755)
        self.assertFalse(Path(".nx").exists())
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            self.command("begin")
            self.assertFalse(cache.ARCHIVE.exists())
        self.command("save")
        self.assertFalse(cache.ARCHIVE.exists())

    def test_corrupt_or_expired_snapshots_stay_cold(self):
        self.prepared()
        self.command("save")
        meta = json.loads(cache.META.read_text())
        meta["created"] -= cache.TTL + 1
        cache.META.write_text(json.dumps(meta))
        self.command("restore")
        self.assertFalse(cache.ARCHIVE.exists())
        self.command("save")
        cache.ARCHIVE.write_bytes(b"bad")
        self.command("restore")
        self.assertFalse(cache.ARCHIVE.exists())
        self.assertEqual(self.output.read_text().splitlines(), ["hit=false", "hit=false"])

    def test_changed_checkout_expires_previous_revision_before_source_runs(self):
        self.prepared()
        self.command("save")
        self.clean()
        self.git("-c", "user.email=test@example.com", "-c", "user.name=test",
                 "commit", "--allow-empty", "-qm", "next revision")
        self.command("begin")
        self.assertFalse(cache.ARCHIVE.exists())
        self.command("restore")
        self.assertEqual(self.output.read_text(), "hit=false\n")

    def test_partial_extraction_restores_clean_checkout(self):
        self.prepared()
        self.command("save")
        self.clean()
        def interrupt(*args, **kwargs):
            self.write("node_modules/partial", "partial")
            raise OSError("interrupted")
        with patch.object(cache, "run_with_disk_reserve", interrupt):
            self.command("restore")
        self.assertEqual(Path("packages/twenty-shared/src/index.ts").read_text(), "before")
        self.assertTrue(Path("packages/twenty-shared/src/obsolete.ts").exists())
        self.assertFalse(Path("node_modules").exists())
        self.assertFalse(cache.ARCHIVE.exists())
        self.assertEqual(self.output.read_text(), "hit=false\n")

    def test_unknown_layout_or_insufficient_space_stays_cold(self):
        self.prepared()
        self.write("unexpected/generated", "legacy")
        self.command("save")
        self.assertFalse(cache.ARCHIVE.exists())
        Path("unexpected/generated").unlink()
        self.assertTrue(cache.BYPASS.exists())
        cache.BYPASS.unlink()
        with patch.object(cache, "RESERVE", 2**60):
            self.command("save")
        self.assertFalse(cache.PART.exists())
        self.assertFalse(cache.ARCHIVE.exists())

    def test_partial_write_cannot_replace_cache(self):
        self.prepared()
        Path("node_modules/random").write_bytes(os.urandom(2**20))
        with patch.object(cache, "CAP", 4096), self.assertRaises(subprocess.CalledProcessError):
            self.command("save")
        self.assertFalse(cache.ARCHIVE.exists())
        self.assertFalse(cache.PART.exists())
        cache.PART.write_bytes(b"stale")
        self.command("begin")
        self.assertFalse(cache.PART.exists())


if __name__ == "__main__":
    unittest.main()
