"""Runs the ``ample`` CLI.

The CLI owns packaging, build detection and the deploy contract (exit 0 live,
1 failed, 2 needs input), so the plugin drives it instead of reimplementing the
upload protocol. The plugin uses one pinned CLI release, downloaded from Ample's
release server on first use and checked against the SHA-256 below; the
``cli_path`` setting points it at a binary of the owner's choosing instead.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CLI_VERSION = "0.1.36"
RELEASE_URL = "https://get.ample.computer/releases"
# From https://get.ample.computer/releases/0.1.36/release.json.
CHECKSUMS = {
    "ample-darwin-arm64": "9101a8941862f240105c179d69f58fa19761ae5fd2c130ca9b8fce0eb441c352",
    "ample-darwin-amd64": "6c044091a10eba97a8605c1c94dcaf0d270a7f589aedb723eb4479aa2940e0da",
    "ample-linux-amd64": "0c5b87467779352331065d496ac719b738c0c2cca03b5a1ee8de21435b70d22c",
    "ample-linux-arm64": "dee6f730aad2eb67d0a11dd7c70e92a48976249202d7e4dfc217255f099ed20d",
    "ample-windows-amd64.exe": "e01aae9c68313d841369987127d899089adf8298dafd1b40088f7d6d7bb4fe37",
}
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024

# Enough for any JSON result and a build log tail.
OUTPUT_CAP = 256 * 1024

# What the CLI gets from Hermes's environment; nothing else, so no other key reaches it.
PASSED_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR",
    # Windows needs these to start a process and find its temp and profile directories.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA",
)


class CliMissing(Exception):
    """No CLI to run: no release for this platform, a failed download, or a bad ``cli_path``."""


@dataclass
class CliResult:
    exit_code: Optional[int]  # None when the process was killed (timeout)
    stdout: str
    stderr: str
    timed_out: bool = False


def asset_name(system: str = sys.platform, machine: str = platform.machine()) -> Optional[str]:
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine.lower())
    if system.startswith("linux"):
        name = f"ample-linux-{arch}" if arch else None
    elif system == "darwin":
        name = f"ample-darwin-{arch}" if arch else None
    elif system in ("win32", "cygwin"):
        # Windows on ARM runs the amd64 build under emulation.
        name = "ample-windows-amd64.exe"
    else:
        name = None
    return name if name in CHECKSUMS else None


Download = Callable[[str, Path], None]


def urllib_download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-ample"})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        total = 0
        while chunk := response.read(1 << 20):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise OSError("download larger than expected")
            out.write(chunk)


class Binary:
    """The pinned CLI under the plugin's data directory, fetched once and verified."""

    def __init__(self, data_dir: Callable[[], Path], *, override: str = "", download: Download = urllib_download,
                 asset: Optional[str] = None) -> None:
        self._data_dir = data_dir
        self._override = override
        self._download = download
        self._asset = asset if asset is not None else asset_name()
        self._lock = threading.Lock()
        self._verified: set[Path] = set()

    def path(self) -> Path:
        if self._override:
            path = Path(self._override).expanduser()
            if not path.is_file():
                raise CliMissing(f"The cli_path setting points at {path}, which is not a file.")
            return path
        if not self._asset:
            raise CliMissing(
                f"Ample publishes no CLI for this platform ({sys.platform}/{platform.machine()}). "
                "Install one and set the plugin's cli_path setting."
            )
        target = self._data_dir() / "bin" / CLI_VERSION / ("ample.exe" if self._asset.endswith(".exe") else "ample")
        with self._lock:
            if target in self._verified:
                return target
            expected = CHECKSUMS[self._asset]
            if target.is_file() and _sha256(target) == expected:
                self._verified.add(target)
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".download-")
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                url = f"{RELEASE_URL}/{CLI_VERSION}/{self._asset}"
                try:
                    self._download(url, tmp)
                except OSError as error:
                    raise CliMissing(f"Could not download the Ample CLI from {url}: {error}.") from None
                if _sha256(tmp) != expected:
                    raise CliMissing(f"The Ample CLI downloaded from {url} does not match its pinned checksum.")
                tmp.chmod(0o755)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            self._verified.add(target)
            return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def cli_env(base: dict, *, token: str, api_url: str, config_path: Path) -> dict:
    """The CLI's environment: a credential for this one run and nothing else from Hermes.

    ``AMPLE_CONFIG_PATH`` points at a file the plugin never writes, so a login the
    owner made with their own ``ample`` cannot swap the account, and the CLI's own
    state stays in the plugin's data directory. Crash reports are off: the plugin
    sends nothing to Ample beyond the commands it runs.
    """
    env = {name: base[name] for name in PASSED_ENV if base.get(name)}
    env.update(
        AMPLE_TOKEN=token,
        AMPLE_API_URL=api_url,
        AMPLE_CONFIG_PATH=str(config_path),
        AMPLE_NO_REPORTS="1",
        DO_NOT_TRACK="1",
        NO_COLOR="1",
    )
    return env


def run(binary: Path, args: list[str], *, env: dict, timeout: float) -> CliResult:
    # stdin is closed so a prompt the CLI might show can never hang the agent.
    try:
        process = subprocess.Popen(
            [str(binary), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        raise CliMissing(f"The Ample CLI at {binary} could not be started.") from None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        timed_out = True
    return CliResult(
        exit_code=None if timed_out else process.returncode,
        stdout=stdout[:OUTPUT_CAP].decode("utf-8", "replace"),
        stderr=stderr[:OUTPUT_CAP].decode("utf-8", "replace"),
        timed_out=timed_out,
    )
