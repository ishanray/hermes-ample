"""Registration: the four tools, the approval hook and the web-apps skill."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from . import cli, schemas
from .account import DEFAULT_API_URL, Account, Settings
from .tools import Folders, Published, Tools, resolve_folder

logger = logging.getLogger(__name__)

TOOLSET = "ample"
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
# Never uploaded, and never the parent of a deploy: credential stores under the home directory.
CREDENTIAL_DIRS = (".ssh", ".aws", ".gnupg", ".config", ".kube", ".docker", ".azure")


def _secret(name: str) -> Optional[str]:
    """Read through Hermes's secret scope, so a multiplexed gateway reads the active profile's value."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get(name)
    return get_secret(name)


def _save_secret(name: str, value: str) -> None:
    """Store a credential the plugin signed up for in Hermes's own .env, where Hermes keeps every key."""
    from hermes_cli.config import save_env_value

    save_env_value(name, value)


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("ample")


def _cwd() -> str:
    cwd = _secret("TERMINAL_CWD") or ""
    return str(Path(os.getcwd(), os.path.expanduser(cwd))) if cwd else os.getcwd()


def _protected() -> list[Path]:
    home = Path.home()
    return [_hermes_home(), *(home / name for name in CREDENTIAL_DIRS)]


class Plugin:
    """One set of tools per Hermes home, so each profile keeps its own Ample account."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._by_home: dict[str, Tools] = {}

    def setting(self, key: str, default: Any) -> Any:
        try:
            value = self.ctx.get_config(key, default=default)
        except Exception:  # noqa: BLE001 - fall back to the default on an unreadable config
            return default
        return default if value is None else value

    def tools(self) -> Tools:
        home = str(_hermes_home())
        with self._lock:
            tools = self._by_home.get(home)
            if tools is None:
                settings = Settings(
                    api_url=str(self.setting("api_url", DEFAULT_API_URL) or DEFAULT_API_URL),
                    signup=bool(self.setting("signup", True)),
                    owner_email=str(self.setting("owner_email", "") or ""),
                )
                account = Account(settings, secret=_secret, save_secret=_save_secret, state=self.ctx.state)
                binary = cli.Binary(_data_dir, override=str(self.setting("cli_path", "") or ""))
                tools = Tools(
                    account,
                    binary,
                    folders=Folders(cwd=_cwd, protected=_protected),
                    config_path=lambda: _data_dir() / "cli" / "config.json",
                    base_env=lambda: dict(os.environ),
                    published=Published(self.ctx.state),
                )
                self._by_home[home] = tools
            return tools

    def approval(self, tool_name: str = "", args: Optional[dict] = None, task_id: str = "", **_: Any) -> Optional[dict]:
        """Ask the user before an app is deleted, and before a folder is published for the first time."""
        try:
            args = args or {}
            if tool_name == "ample_app_delete":
                deployment_id = str(args.get("deployment_id", ""))
                # Scoped to the one app, so an "always" answer never covers other deletes.
                return {
                    "action": "approve",
                    "message": f"Delete the Ample app {deployment_id}? Its URL stops working.",
                    "rule_key": f"ample:delete:{deployment_id}",
                }
            if tool_name == "ample_deploy" and self.setting("confirm_new_sites", True):
                folder = resolve_folder(str(args.get("path") or ""), Folders(cwd=_cwd, protected=_protected))
                # A refused path is reported by the tool itself. Only a folder the user
                # published before (recorded in plugin state) skips the question; files in
                # the folder, such as an .ample/ the agent created, never do.
                if isinstance(folder, Path) and not Published(self.ctx.state).contains(folder):
                    return {
                        "action": "approve",
                        "message": f"Publish {folder} on the public internet with Ample? Everything in the folder is uploaded.",
                        "rule_key": f"ample:publish:{folder}",
                    }
        except Exception:  # noqa: BLE001 - when unsure, ask
            logger.debug("ample approval check failed", exc_info=True)
            return {"action": "approve", "message": "Run this Ample action?", "rule_key": f"ample:{tool_name}"}
        return None


def _handler(plugin: Plugin, method: str):
    def handle(args: Optional[dict] = None, **kwargs: Any) -> str:
        try:
            return getattr(plugin.tools(), method)(args or {}, **kwargs)
        except Exception as error:  # noqa: BLE001 - a tool never raises
            logger.debug("ample %s failed", method, exc_info=True)
            return json.dumps({"ok": False, "error": f"The Ample plugin failed: {type(error).__name__}: {error}"})

    return handle


def register(ctx: Any) -> None:
    plugin = Plugin(ctx)
    tools = (
        (schemas.DEPLOY, "deploy", "🚀"),
        (schemas.LOGS, "logs", "📜"),
        (schemas.APPS, "apps", "📋"),
        (schemas.DELETE, "delete", "🗑️"),
    )
    for schema, method, emoji in tools:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=_handler(plugin, method),
                          description=schema["description"], emoji=emoji)
    ctx.register_hook("pre_tool_call", plugin.approval)
    skill = SKILLS_DIR / "web-apps" / "SKILL.md"
    if skill.is_file():
        ctx.register_skill("web-apps", skill)
