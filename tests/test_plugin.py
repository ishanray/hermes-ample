import json
from pathlib import Path

import pytest

import hermes_ample.plugin as plugin_module
from hermes_ample.plugin import Plugin, register
from hermes_ample.tools import Folders, Published, Tools
from tests.helpers import FakeApi, FakeCli, MemoryState, Secrets, make_account, ok

LIVE = {"status": "live", "url": "https://site.example", "deployment_id": "dep_1"}


class FixedBinary:
    def path(self):
        return Path("/opt/ample")


class FakeContext:
    def __init__(self, config=None):
        self.config = config or {}
        self.state = MemoryState()
        self.tools = {}
        self.hooks = {}
        self.skills = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, toolset, schema, handler, description="", emoji="", **_):
        assert toolset == "ample" and schema["name"] == name and description
        self.tools[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_skill(self, name, path):
        self.skills[name] = Path(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(plugin_module, "_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(plugin_module, "_cwd", lambda: str(work))
    monkeypatch.setattr(plugin_module, "_protected", lambda: [hermes_home])
    return work


def test_register_adds_the_tools_hook_and_skill():
    ctx = FakeContext()
    register(ctx)
    assert set(ctx.tools) == {"ample_deploy", "ample_logs", "ample_apps", "ample_app_delete"}
    assert set(ctx.hooks) == {"pre_tool_call"}
    assert ctx.skills["web-apps"].read_text().startswith("---\nname: web-apps")


def test_the_manifest_declares_what_register_registers():
    manifest = (Path(__file__).parent.parent / "plugin.yaml").read_text()
    ctx = FakeContext()
    register(ctx)
    for name in ctx.tools:
        assert f"  - {name}\n" in manifest
    assert "  - pre_tool_call\n" in manifest


def test_deleting_an_app_always_asks_for_that_app_only(home):
    plugin = Plugin(FakeContext())
    directive = plugin.approval("ample_app_delete", {"deployment_id": "dep_1"}, "task")
    assert directive["action"] == "approve" and "dep_1" in directive["message"]
    # An "always" answer is scoped to the one app, never to every future delete.
    assert directive["rule_key"] == "ample:delete:dep_1"
    assert plugin.approval("ample_app_delete", {"deployment_id": "dep_2"}, "task")["rule_key"] == "ample:delete:dep_2"


def test_a_folder_the_agent_marked_as_deployed_still_asks(home):
    # The agent can create .ample/ (or ample.toml) itself with a file or terminal call;
    # that must not stand in for the user's yes.
    folder = home / "Documents"
    (folder / ".ample").mkdir(parents=True)
    (folder / ".ample" / "account.json").write_text('{"account_id": "acc_x"}')
    (folder / "ample.toml").write_text("version = 1\n")
    directive = Plugin(FakeContext()).approval("ample_deploy", {"path": "Documents"}, "task")
    assert directive["action"] == "approve"
    assert str(folder.resolve()) in directive["message"]


def test_a_folder_published_through_the_tool_is_not_asked_about_again(home):
    folder = home / "site"
    folder.mkdir()
    (folder / "index.html").write_text("<h1>hi</h1>")
    ctx = FakeContext()
    plugin = Plugin(ctx)
    assert plugin.approval("ample_deploy", {"path": "site"}, "task")["action"] == "approve"

    # The user approved, so the tool ran and deployed it.
    fake = FakeCli(lambda args: ok(LIVE if args[2] == "deploy" else {"services": [{"path": "."}]}))
    api = FakeApi()
    tools = Tools(make_account(api, Secrets(), ctx.state), FixedBinary(),
                  folders=Folders(cwd=lambda: str(home), home=home.parent / "elsewhere"),
                  config_path=lambda: home / "cli.json", base_env=dict,
                  published=Published(ctx.state), run=fake)
    assert json.loads(tools.deploy({"path": "site"}))["ok"] is True

    assert plugin.approval("ample_deploy", {"path": "site"}, "task") is None
    assert plugin.approval("ample_deploy", {"path": str(folder)}, "task") is None
    other = home / "other"
    other.mkdir()
    assert plugin.approval("ample_deploy", {"path": "other"}, "task")["action"] == "approve"


def test_a_symlink_to_an_unpublished_folder_still_asks(home):
    real = home / "private"
    real.mkdir()
    published = home / "site"
    published.mkdir()
    ctx = FakeContext()
    Published(ctx.state).add(published.resolve())
    published.rmdir()
    published.symlink_to(real, target_is_directory=True)
    assert Plugin(ctx).approval("ample_deploy", {"path": "site"}, "task")["action"] == "approve"


def test_the_first_publish_prompt_can_be_turned_off(home):
    (home / "site").mkdir()
    plugin = Plugin(FakeContext({"confirm_new_sites": False}))
    assert plugin.approval("ample_deploy", {"path": "site"}) is None


def test_other_tools_and_refused_paths_pass_through(home):
    plugin = Plugin(FakeContext())
    assert plugin.approval("terminal", {"command": "ls"}) is None
    assert plugin.approval("ample_apps", {}) is None
    # The tool reports a missing folder itself.
    assert plugin.approval("ample_deploy", {"path": "nope"}) is None


def test_a_failure_while_setting_up_comes_back_as_an_error(monkeypatch):
    ctx = FakeContext()
    register(ctx)

    def broken():
        raise RuntimeError("no hermes home")

    monkeypatch.setattr(plugin_module, "_hermes_home", broken)
    result = json.loads(ctx.tools["ample_apps"]({}))
    assert result["ok"] is False and "no hermes home" in result["error"]


def test_each_hermes_home_gets_its_own_account(tmp_path, monkeypatch):
    homes = iter([tmp_path / "a", tmp_path / "b", tmp_path / "a"])
    monkeypatch.setattr(plugin_module, "_hermes_home", lambda: next(homes))
    plugin = Plugin(FakeContext())
    first, second, again = plugin.tools(), plugin.tools(), plugin.tools()
    assert first is not second and first is again
