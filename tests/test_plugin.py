import json
from pathlib import Path

import pytest

import hermes_ample.plugin as plugin_module
from hermes_ample.plugin import Plugin, register
from tests.helpers import MemoryState


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


def test_deleting_an_app_always_asks(home):
    plugin = Plugin(FakeContext())
    directive = plugin.approval("ample_app_delete", {"deployment_id": "dep_1"}, "task")
    assert directive["action"] == "approve" and "dep_1" in directive["message"]


def test_publishing_a_new_folder_asks_and_a_redeploy_does_not(home):
    folder = home / "site"
    folder.mkdir()
    plugin = Plugin(FakeContext())
    directive = plugin.approval("ample_deploy", {"path": "site"}, "task")
    assert directive["action"] == "approve" and str(folder.resolve()) in directive["message"]
    (folder / ".ample").mkdir()
    assert plugin.approval("ample_deploy", {"path": "site"}, "task") is None


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
