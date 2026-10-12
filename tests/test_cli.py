import hashlib
import sys
from pathlib import Path

import pytest

from hermes_ample import cli


def fake_release(content: bytes):
    calls = []

    def download(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(content)

    return download, calls


def test_the_pinned_cli_is_downloaded_once_and_verified(tmp_path, monkeypatch):
    content = b"#!/bin/sh\necho ample\n"
    monkeypatch.setitem(cli.CHECKSUMS, "ample-linux-amd64", hashlib.sha256(content).hexdigest())
    download, calls = fake_release(content)
    binary = cli.Binary(lambda: tmp_path, download=download, asset="ample-linux-amd64")

    path = binary.path()
    assert path == tmp_path / "bin" / cli.CLI_VERSION / "ample"
    assert path.read_bytes() == content
    assert calls == [f"https://get.ample.computer/releases/{cli.CLI_VERSION}/ample-linux-amd64"]
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o111

    # A new process finds the verified copy without downloading again.
    again = cli.Binary(lambda: tmp_path, download=download, asset="ample-linux-amd64")
    assert again.path() == path
    assert len(calls) == 1


def test_a_download_that_does_not_match_the_checksum_is_refused(tmp_path, monkeypatch):
    monkeypatch.setitem(cli.CHECKSUMS, "ample-linux-amd64", "0" * 64)
    download, _ = fake_release(b"tampered")
    binary = cli.Binary(lambda: tmp_path, download=download, asset="ample-linux-amd64")
    with pytest.raises(cli.CliMissing, match="does not match"):
        binary.path()
    assert not (tmp_path / "bin" / cli.CLI_VERSION / "ample").exists()
    assert list((tmp_path / "bin" / cli.CLI_VERSION).iterdir()) == []


def test_a_modified_copy_is_downloaded_again(tmp_path, monkeypatch):
    content = b"real"
    monkeypatch.setitem(cli.CHECKSUMS, "ample-linux-amd64", hashlib.sha256(content).hexdigest())
    target = tmp_path / "bin" / cli.CLI_VERSION / "ample"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"changed")
    download, calls = fake_release(content)
    assert cli.Binary(lambda: tmp_path, download=download, asset="ample-linux-amd64").path().read_bytes() == content
    assert len(calls) == 1


def test_cli_path_overrides_the_download(tmp_path):
    own = tmp_path / "ample"
    own.write_text("")
    assert cli.Binary(lambda: tmp_path, override=str(own)).path() == own
    with pytest.raises(cli.CliMissing, match="not a file"):
        cli.Binary(lambda: tmp_path, override=str(tmp_path / "missing")).path()


def test_unknown_platforms_have_no_asset():
    assert cli.asset_name("linux", "x86_64") == "ample-linux-amd64"
    assert cli.asset_name("darwin", "arm64") == "ample-darwin-arm64"
    assert cli.asset_name("win32", "AMD64") == "ample-windows-amd64.exe"
    assert cli.asset_name("linux", "riscv64") is None
    assert cli.asset_name("freebsd14", "amd64") is None
    with pytest.raises(cli.CliMissing, match="no CLI for this platform"):
        cli.Binary(lambda: Path("."), asset="").path()


def test_the_cli_gets_only_its_credential_and_no_other_secret(tmp_path):
    env = cli.cli_env(
        {"PATH": "/usr/bin", "HOME": "/home/u", "OPENAI_API_KEY": "sk-secret", "AMPLE_CLIENT_SECRET": "s"},
        token="tok", api_url="https://api.example", config_path=tmp_path / "config.json",
    )
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "AMPLE_TOKEN": "tok",
        "AMPLE_API_URL": "https://api.example",
        "AMPLE_CONFIG_PATH": str(tmp_path / "config.json"),
        "AMPLE_NO_REPORTS": "1",
        "DO_NOT_TRACK": "1",
        "NO_COLOR": "1",
    }


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_run_returns_output_and_exit_code(tmp_path):
    script = tmp_path / "ample"
    script.write_text('#!/bin/sh\necho "{\\"args\\": \\"$*\\"}"\necho oops >&2\nexit 2\n')
    script.chmod(0o755)
    result = cli.run(script, ["deploy", "x"], env={"PATH": "/usr/bin:/bin"}, timeout=10)
    assert result.exit_code == 2
    assert result.stdout.strip() == '{"args": "deploy x"}'
    assert result.stderr.strip() == "oops"
    assert not result.timed_out


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_run_stops_a_command_that_takes_too_long(tmp_path):
    script = tmp_path / "ample"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    result = cli.run(script, [], env={"PATH": "/usr/bin:/bin"}, timeout=0.5)
    assert result.timed_out
    assert result.exit_code is None
