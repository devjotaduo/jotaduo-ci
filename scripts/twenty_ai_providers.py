"""Transferência da configuração de IA de teste e homologação."""

import argparse
import base64
import grp
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PROVIDERS = ("litellm", "cliproxy")
ENVIRONMENTS = {
    "teste": ("twenty-ai-config-teste", Path("/etc/twenty-test-image")),
    "homologacao": ("twenty-ai-config-homologacao", Path("/etc/twenty-teste")),
}
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_CATALOG_BYTES = 4 * 1024 * 1024
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class ConfigurationError(Exception):
    """Uma falha que pode aparecer nos logs sem incluir valores de entrada."""


class NoCatalogRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def check_model_catalogs(payload):
    opener = build_opener(ProxyHandler({}), NoCatalogRedirect())
    for name, provider in json.loads(payload).items():
        request = Request(
            provider["baseUrl"].rstrip("/") + "/models",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {provider['apiKey']}",
            },
        )
        try:
            with opener.open(request, timeout=10) as response:
                body = response.read(MAX_CATALOG_BYTES + 1)
            if len(body) > MAX_CATALOG_BYTES:
                raise ValueError
            data = json.loads(body)["data"]
            if not isinstance(data, list):
                raise ValueError
        except (HTTPException, OSError, ValueError, KeyError, TypeError, RecursionError):
            raise ConfigurationError(f"Não foi possível validar o catálogo autenticado de {name}.") from None

        available = {
            item["id"] for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if not set(provider["models"]).issubset(available):
            raise ConfigurationError(f"Há modelos configurados fora do catálogo autorizado de {name}.")


def is_hostname(value):
    return len(value) <= 253 and all(DNS_LABEL.fullmatch(label) for label in value.split("."))


def is_https_url(value):
    if not isinstance(value, str) or not value.startswith("https://") or not value.isascii():
        return False
    if re.search(r"[\x00-\x20\x7f\\?#]", value):
        return False
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        return False
    return (
        url.hostname is not None
        and is_hostname(url.hostname)
        and url.username is None
        and url.password is None
        and not url.netloc.endswith(":")
        and (port is None or port > 0)
    )


def is_token(value):
    return (
        isinstance(value, str)
        and bool(value)
        and all(character.isprintable() and not character.isspace() for character in value)
    )


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ConfigurationError("A configuração contém um campo repetido.")
        result[name] = value
    return result


def checked_payload(config):
    if not isinstance(config, dict) or set(config) != set(PROVIDERS):
        raise ConfigurationError("A configuração precisa de LiteLLM e CLIProxy.")
    for name, provider in config.items():
        if not isinstance(provider, dict) or set(provider) != {"baseUrl", "apiKey", "models"}:
            raise ConfigurationError(f"Campos inválidos para {name}.")
        if not is_https_url(provider["baseUrl"]):
            raise ConfigurationError(f"URL HTTPS inválida para {name}.")
        if not is_token(provider["apiKey"]):
            raise ConfigurationError(f"Chave ausente ou inválida para {name}.")
        models = provider["models"]
        if not isinstance(models, list) or not models or not all(is_token(model) for model in models):
            raise ConfigurationError(f"Modelos ausentes ou inválidos para {name}.")
        if len(set(models)) != len(models):
            raise ConfigurationError(f"Modelos repetidos para {name}.")
    payload = (json.dumps(config, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ConfigurationError("A configuração excede o limite de 32 KiB.")
    return payload


def payload_from_environment(env):
    config = {}
    for name in PROVIDERS:
        prefix = name.upper()
        models = env.get(f"{prefix}_MODELS_JSON", "")
        try:
            model_ids = json.loads(models)
        except (ValueError, RecursionError):
            raise ConfigurationError(f"Lista JSON de modelos inválida para {name}.") from None
        config[name] = {
            "baseUrl": env.get(f"{prefix}_BASE_URL", ""),
            "apiKey": env.get(f"{prefix}_API_KEY", ""),
            "models": model_ids,
        }
    return checked_payload(config)


def payload_from_stream(stream):
    raw = stream.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ConfigurationError("A configuração excede o limite de 32 KiB.")
    try:
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise ConfigurationError("A configuração recebida precisa ser JSON UTF-8.") from None
    return checked_payload(config)


def write_configuration(directory, payload, file_gid):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        folder = os.fstat(descriptor)
        if (
            folder.st_uid != os.geteuid()
            or folder.st_gid not in (0, file_gid)
            or stat.S_IMODE(folder.st_mode) not in (0o700, 0o750)
        ):
            raise ConfigurationError("Propriedade, grupo ou modo do diretório receptor inválido.")
        try:
            previous = os.stat("providers.json", dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None and (
            not stat.S_ISREG(previous.st_mode)
            or previous.st_uid != os.geteuid()
            or previous.st_gid != file_gid
            or stat.S_IMODE(previous.st_mode) not in (0o600, 0o640)
        ):
            raise ConfigurationError("Propriedade, grupo ou modo do arquivo receptor inválido.")
        fd, temporary = tempfile.mkstemp(prefix=".providers-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as file:
                os.fchown(file.fileno(), -1, file_gid)
                file.write(payload)
                file.flush()
                os.fchmod(file.fileno(), 0o640)
                os.fsync(file.fileno())
            os.replace(temporary, directory / "providers.json")
            os.fsync(descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        os.close(descriptor)


def checked_pins(host, pins):
    lines = pins.splitlines()
    if not lines or len(lines) > 4:
        raise ConfigurationError("Informe até quatro chaves SSH de host fixadas.")
    for line in lines:
        fields = line.split()
        if len(fields) != 3 or fields[:2] != [host, "ssh-ed25519"]:
            raise ConfigurationError("O pin SSH precisa indicar o host exato e uma chave ed25519.")
        try:
            blob = base64.b64decode(fields[2], validate=True)
        except ValueError:
            raise ConfigurationError("Chave SSH de host inválida.") from None
        if len(blob) != 51 or not blob.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
            raise ConfigurationError("Chave SSH de host inválida.")
    return "\n".join(lines) + "\n"


def send(environment, env):
    user, _ = ENVIRONMENTS[environment]
    payload = payload_from_environment(env)
    host = env.get("TWENTY_CONFIG_SSH_HOST", "")
    if not is_hostname(host):
        raise ConfigurationError("Host SSH ausente ou inválido.")
    pins = checked_pins(host, env.get("TWENTY_CONFIG_SSH_KNOWN_HOSTS", ""))
    private_key = env.get("TWENTY_CONFIG_SSH_KEY", "").strip()
    if not private_key:
        raise ConfigurationError("Falta a chave SSH deste ambiente.")
    check_model_catalogs(payload)
    with tempfile.TemporaryDirectory(prefix="twenty-ai-") as tmp:
        key_path = Path(tmp) / "identity"
        pins_path = Path(tmp) / "known_hosts"
        with open(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as key_file:
            key_file.write(private_key + "\n")
        pins_path.write_text(pins, encoding="ascii")
        result = subprocess.run(
            [
                "/usr/bin/ssh", "-F", "/dev/null", "-T",
                "-oBatchMode=yes", "-oIdentitiesOnly=yes", "-oIdentityAgent=none",
                "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no", "-oGlobalKnownHostsFile=/dev/null",
                f"-oUserKnownHostsFile={pins_path}", "-oHostKeyAlgorithms=ssh-ed25519",
                "-oClearAllForwardings=yes", "-oForwardAgent=no", "-oPermitLocalCommand=no",
                "-oConnectTimeout=10", "-i", str(key_path), "-l", user, "--", host,
            ],
            input=payload,
            env={"PATH": os.defpath, "LC_ALL": "C"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=40,
            check=False,
        )
        if result.returncode:
            raise ConfigurationError("O receptor SSH recusou a atualização. Verifique o pin, a chave e a configuração do host.")


def input_timeout(_signum, _frame):
    raise ConfigurationError("O tempo para receber a configuração expirou.")


def receive(environment):
    if os.geteuid() != 0:
        raise ConfigurationError("O receptor exige o comando fixo do sudo.")
    _, directory = ENVIRONMENTS[environment]
    signal.signal(signal.SIGALRM, input_timeout)
    signal.alarm(25)
    try:
        payload = payload_from_stream(sys.stdin.buffer)
    finally:
        signal.alarm(0)
    try:
        file_gid = 0 if environment == "teste" else grp.getgrnam("twenty-teste").gr_gid
    except KeyError:
        raise ConfigurationError("O grupo do serviço de homologação precisa estar provisionado.") from None
    write_configuration(directory, payload, file_gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("send", "receive"))
    parser.add_argument("environment", choices=ENVIRONMENTS)
    args = parser.parse_args()
    try:
        if args.operation == "send":
            send(args.environment, os.environ)
        else:
            receive(args.environment)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Falha no transporte ou na gravação protegida.", file=sys.stderr)
        return 1
    print("Configuração de provedores atualizada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
