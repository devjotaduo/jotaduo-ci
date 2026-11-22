#!/usr/bin/env python3
"""Local preparation cache for the dedicated Linux CI runner."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

MIB = 1024**2
CAP = 1536 * MIB
PATCH_CAP = 16 * MIB
RESERVE = 12 * 1024 * MIB
TTL = 6 * 3600
ROOT = Path("/tmp") / f"jotaduo-ci-prepared-{os.getuid()}"
ARCHIVE = ROOT / "prepared.tar.zst"
META = ROOT / "prepared.json"
SLOT = ROOT / "slot"
PART = ROOT / "creating.tar.zst"
BYPASS = ROOT / "bypass"


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def fingerprint(*values):
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def scope():
    fields = ("GITHUB_REPOSITORY_ID", "GITHUB_WORKFLOW_SHA", "GITHUB_RUN_ID",
              "GITHUB_RUN_ATTEMPT", "RUNNER_NAME", "CACHE_NONCE")
    values = [os.environ[key] for key in fields]
    if not all(values):
        raise ValueError("incomplete generation")
    return fingerprint(git("rev-parse", "HEAD").decode().strip(), os.getcwd(), *values)


def identity():
    return fingerprint(scope(), platform.machine(), subprocess.check_output(["node", "--version"]).decode())


def clear():
    for path in (ARCHIVE, META, PART, BYPASS):
        path.unlink(missing_ok=True)


def installed(path):
    return path.name == "node_modules" or path.as_posix() == ".yarn/install-state.gz"


def shared(path):
    return path.parts[:2] == ("packages", "twenty-shared")


def inputs(directory):
    # Inspect *all* untracked files, including ignored outputs. Unknown layouts stay cold.
    records = git("ls-files", "--others", "--directory", "--no-empty-directory", "-z")
    paths = []
    for name in records.split(b"\0"):
        if not name:
            continue
        path = Path(os.fsdecode(name))
        if path.parts[0] == ".nx":
            continue
        if path == Path(".yarn"):
            for item in path.iterdir():
                if item.name == "install-state.gz":
                    paths.append(item)
                elif not git("ls-files", "--", str(item)):
                    return None
        elif installed(path) or shared(path):
            paths.append(path)
        else:
            return None
    changed = git("diff", "--name-only", "--no-renames", "-z", "HEAD")
    if any(not shared(Path(os.fsdecode(p))) for p in changed.split(b"\0") if p):
        return None
    # Capture generated/tracked indices and manifests, including deletions and mode changes.
    def limit_patch():
        resource.setrlimit(resource.RLIMIT_FSIZE, (PATCH_CAP, PATCH_CAP))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with (directory / "tracked.patch").open("wb") as patch:
        result = subprocess.run(["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
                                stdout=patch, stderr=subprocess.DEVNULL, preexec_fn=limit_patch)
        if result.returncode:
            return None
    if not Path("node_modules/.yarn-state.yml").is_file() or not Path(".yarn/install-state.gz").is_file():
        return None
    return paths


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            checksum.update(block)
    return checksum.hexdigest()


def run_with_disk_reserve(command, lock, *, output=None, max_file=False):
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if max_file:
            resource.setrlimit(resource.RLIMIT_FSIZE, (CAP, CAP))
    # Inherit flock so a cancelled parent cannot leave a compressor racing a new generation.
    proc = subprocess.Popen(command, stdout=output, stderr=subprocess.DEVNULL, preexec_fn=limits, start_new_session=True,
                            pass_fds=(lock,))
    try:
        while proc.poll() is None:
            if shutil.disk_usage(ROOT).free < RESERVE:
                raise OSError("disk reserve reached")
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, command)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def save(directory, key, lock):
    if META.exists() or (BYPASS.exists() and BYPASS.read_text() == key):
        return
    ARCHIVE.unlink(missing_ok=True)
    if shutil.disk_usage(ROOT).free < RESERVE + CAP:
        print("Cache bypassed: insufficient disk reserve.")
        return
    paths = inputs(directory)
    if paths is None:
        BYPASS.write_text(key)
        print("Cache bypassed: preparation layout is unsupported.")
        return
    # The first member carries the tracked diff. All others are relative to the checkout.
    manifest = directory / "members"
    manifest.write_bytes(b"".join(os.fsencode(p) + b"\0" for p in paths))
    try:
        with PART.open("w+b") as target:
            # Reserve the maximum before writing. Concurrent jobs on this volume see the allocation.
            os.posix_fallocate(target.fileno(), 0, CAP)
            if shutil.disk_usage(ROOT).free < RESERVE:
                return
            run_with_disk_reserve(
                ["tar", "--one-file-system", "--use-compress-program=zstd -q -1 -T1",
                 "-cf", "-", "-C", str(directory), "tracked.patch",
                 "-C", os.getcwd(), "--null", "-T", str(manifest)], lock, output=target, max_file=True)
            target.truncate(target.tell())
        # Account for the blocks occupied by the installed tree on this runner.
        size = int(subprocess.check_output(
            ["du", "--block-size=1", "--summarize", "--total", "--files0-from", str(manifest)],
            stderr=subprocess.DEVNULL
        ).splitlines()[-1].split(b"\t")[0])
        meta = {"identity": key, "created": time.time(), "bytes": size, "sha256": digest(PART)}
        PART.replace(ARCHIVE)
        META.write_text(json.dumps(meta))
        print(f"Prepared cache saved ({ARCHIVE.stat().st_size // MIB} MiB).")
    except (OSError, subprocess.CalledProcessError):
        clear()
        BYPASS.write_text(key)
        raise
    finally:
        PART.unlink(missing_ok=True)


def restore(directory, key, lock):
    if not META.exists():
        return False
    try:
        meta = json.loads(META.read_text())
    except (ValueError, OSError):
        clear()
        return False
    if (not isinstance(meta, dict) or meta.get("identity") != key
            or not isinstance(meta.get("created"), (int, float))
            or not isinstance(meta.get("bytes"), int) or meta["bytes"] < 0
            or time.time() - meta["created"] >= TTL
            or not ARCHIVE.is_file() or ARCHIVE.stat().st_size > CAP
            or shutil.disk_usage(Path.cwd()).free < RESERVE + meta["bytes"]
            or digest(ARCHIVE) != meta.get("sha256")):
        clear()
        return False
    if git("status", "--porcelain", "--untracked-files=no"):
        return False
    # Extract the patch separately so no control file is written to the source tree.
    try:
        patch = directory / "tracked.patch"
        with patch.open("wb") as output:
            subprocess.run(["tar", "--zstd", "-xOf", str(ARCHIVE), "tracked.patch",
                            "--occurrence=1"], stdout=output, stderr=subprocess.DEVNULL, check=True)
        if patch.stat().st_size:
            subprocess.run(["git", "apply", "--binary", str(patch)], stderr=subprocess.DEVNULL, check=True)
        run_with_disk_reserve(["tar", "--zstd", "--no-same-owner", "--same-permissions", "--anchored",
                               "--exclude=tracked.patch", "-xf", str(ARCHIVE)], lock)
    except (OSError, subprocess.CalledProcessError):
        clear()
        # Actions has just checked out this disposable workspace. Undo only this partial restore.
        subprocess.run(["git", "reset", "--hard", "--quiet", "HEAD"], check=True)
        subprocess.run(["git", "clean", "-ffdxq"], check=True)
        return False
    return True


def main():
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("unsafe cache root")
    lock = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        command = sys.argv[1]
        generation = scope()
        previous = SLOT.read_text() if SLOT.exists() else ""
        if command == "begin":
            if previous != generation or (META.exists() and time.time() - META.stat().st_mtime >= TTL):
                clear()
            PART.unlink(missing_ok=True)
            if not META.exists():
                ARCHIVE.unlink(missing_ok=True)
            SLOT.write_text(generation)
            return
        if previous != generation:
            return
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as temp:
            directory = Path(temp)
            key = identity()
            started = time.monotonic()
            if command == "restore":
                hit = restore(directory, key, lock)
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    print(f"hit={'true' if hit else 'false'}", file=output)
                print(f"Prepared cache {'hit' if hit else 'miss'} ({time.monotonic() - started:.1f} s).")
            elif command == "save":
                save(directory, key, lock)
            else:
                raise ValueError("unknown cache operation")
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit("Private preparation cache unavailable.")
