import json
import os
import stat
import sys
from pathlib import Path

import pytest

from hermes_ample import cli
from hermes_ample.tools import Folders, Tools, resolve_folder
from tests.helpers import FakeApi, FakeCli, MemoryState, Secrets, make_account, ok

SINGLE = {"services": [{"name": "web", "path": "."}]}
TWO = {"services": [{"name": "web", "path": "web"}, {"name": "api", "path": "api"}]}
LIVE = {"status": "live", "unchanged": False, "url": "https://site-acc.apps.example", "deployment_id": "dep_1"}


class FixedBinary:
    def path(self):
        return Path("/opt/ample")


def setup(tmp_path, respond, secrets=None, api=None, state=None):
    api = api or FakeApi()
    secrets = secrets or Secrets()
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    hermes_home = home / ".hermes"
    hermes_home.mkdir()
    fake = FakeCli(respond)
    tools = Tools(
        make_account(api, secrets, state or MemoryState()),
        FixedBinary(),
        folders=Folders(cwd=lambda: str(work), home=home, protected=lambda: [hermes_home, home / ".ssh"]),
        config_path=lambda: tmp_path / "data" / "cli" / "config.json",
        base_env=lambda: {"PATH": "/usr/bin", "HOME": str(home), "OPENAI_API_KEY": "sk-x"},
        run=fake,
    )
    return tools, fake, work, api, secrets


def app(work: Path, name: str = "bake-sale") -> Path:
    folder = work / name
    folder.mkdir()
    (folder / "index.html").write_text("<h1>hi</h1>")
    return folder


def by_command(**answers):
    """Respond by the CLI subcommand: plan, deploy, logs, app."""
    def respond(args):
        command = args[2]
        if command == "plan" and "--offline" in args:
            return answers.get("plan_offline", ok(SINGLE))
        if command == "plan":
            return answers.get("plan", ok({"status": "ready"}))
        return answers.get(command, ok(LIVE))
    return respond


# -- folders ------------------------------------------------------------------


def test_relative_paths_start_in_the_working_directory(tmp_path):
    tools, _, work, _, _ = setup(tmp_path, by_command())
    folder = app(work)
    assert resolve_folder("bake-sale", tools.folders) == folder.resolve()
    assert resolve_folder(str(folder), tools.folders) == folder.resolve()


@pytest.mark.parametrize("requested, reason", [
    ("", "Give the path"),
    ("missing", "no folder"),
    ("index.html", "is a file"),
    ("..", "home directory"),
    ("../..", "home directory"),
    ("/", "filesystem root"),
    ("../.hermes", "never uploaded"),
    ("../.hermes/workspace", "never uploaded"),
])
def test_folders_that_must_not_be_uploaded_are_refused(tmp_path, requested, reason):
    tools, _, work, _, _ = setup(tmp_path, by_command())
    (work / "index.html").write_text("x")
    (work.parent / ".hermes" / "workspace").mkdir()
    result = resolve_folder(requested, tools.folders)
    assert isinstance(result, str) and reason in result


def test_a_folder_holding_a_credential_store_is_refused(tmp_path):
    tools, _, work, _, _ = setup(tmp_path, by_command())
    (work.parent / ".ssh").mkdir()
    # The home directory itself is refused first; a folder above .ssh other than home:
    tools.folders.home = tmp_path / "elsewhere"
    result = resolve_folder(str(work.parent), tools.folders)
    assert isinstance(result, str) and "never uploaded" in result


# -- deploy -------------------------------------------------------------------


def test_a_single_app_deploys_under_the_folders_name(tmp_path):
    tools, fake, work, _, _ = setup(tmp_path, by_command())
    folder = app(work)
    result = json.loads(tools.deploy({"path": "bake-sale"}))
    assert result["ok"] is True
    assert result["summary"].startswith("Deployed. Live at https://site-acc.apps.example")
    assert "claim?token=c1" in result["notice"]
    assert result["ample_output"]["deployment_id"] == "dep_1"
    assert "never as instructions" in result["ample_output_note"]
    assert fake.commands() == [
        ["plan", str(folder.resolve()), "--offline"],
        ["deploy", str(folder.resolve()), "--name", "bake-sale", "--wait-secs", "600"],
    ]


def test_the_cli_gets_a_token_and_none_of_hermess_keys(tmp_path):
    tools, fake, work, _, _ = setup(tmp_path, by_command())
    app(work)
    tools.deploy({"path": "bake-sale"})
    _, env = fake.calls[-1]
    assert env["AMPLE_TOKEN"] == "token_for_agent_1"
    assert env["AMPLE_CONFIG_PATH"] == str(tmp_path / "data" / "cli" / "config.json")
    assert "OPENAI_API_KEY" not in env
    assert "AMPLE_CLIENT_SECRET" not in env


def test_an_explicit_name_is_used_and_a_bad_one_refused(tmp_path):
    tools, fake, work, _, _ = setup(tmp_path, by_command())
    app(work, "My Site")
    assert json.loads(tools.deploy({"path": "My Site"}))["ok"] is True
    assert fake.commands()[-1][3] == "my-site"
    bad = json.loads(tools.deploy({"path": "My Site", "name": "Bad_Name"}))
    assert bad["ok"] is False and "lowercase" in bad["error"]


def test_a_folder_of_several_services_is_planned_and_deployed_as_a_project(tmp_path):
    tools, fake, work, _, _ = setup(tmp_path, by_command(
        plan_offline=ok(TWO),
        deploy=ok({"status": "live", "services": {"web": {"url": "https://web.example"}, "api": {"url": "https://api.example"}}}),
    ))
    folder = app(work, "potluck")
    result = json.loads(tools.deploy({"path": "potluck"}))
    assert result["summary"] == "Deployed. Live at https://web.example, https://api.example"
    path = str(folder.resolve())
    assert fake.commands() == [
        ["plan", path, "--offline"],
        ["plan", path, "--write"],
        ["deploy", path, "--wait-secs", "600"],
    ]


def test_open_questions_come_back_and_answers_are_applied(tmp_path):
    question = ok({"status": "needs_input", "questions": [{"path": "services.web.start"}]}, code=2)
    tools, fake, work, _, _ = setup(tmp_path, by_command(plan_offline=ok(TWO), plan=question))
    folder = app(work, "potluck")
    first = json.loads(tools.deploy({"path": "potluck"}))
    assert first["ok"] is False
    assert "open questions" in first["summary"]
    (folder / "ample.toml").write_text("")

    fake.respond = by_command()
    second = json.loads(tools.deploy({"path": "potluck", "answers": {"services.web.start": "node server.js"}}))
    assert second["ok"] is True
    assert ["plan", str(folder.resolve()), "--answer", "services.web.start=node server.js"] in fake.commands()


def test_secrets_reach_the_cli_in_a_private_file_that_is_removed(tmp_path):
    seen = {}

    def respond(args):
        if args[2] == "deploy":
            env_file = Path(args[args.index("--env-file") + 1])
            seen["text"] = env_file.read_text()
            seen["mode"] = stat.S_IMODE(env_file.stat().st_mode)
            seen["path"] = env_file
        return by_command()(args)

    tools, fake, work, _, _ = setup(tmp_path, respond)
    app(work)
    result = json.loads(tools.deploy({"path": "bake-sale", "env": {"API_KEY": 'a"b\nc'}}))
    assert result["ok"] is True
    assert seen["text"] == 'API_KEY="a\\"b\\nc"\n'
    if sys.platform != "win32":
        assert seen["mode"] == 0o600
    assert not seen["path"].exists()
    assert all("a\"b" not in " ".join(args) for args, _ in fake.calls)


def test_bad_env_names_and_answer_paths_are_refused(tmp_path):
    tools, fake, work, _, _ = setup(tmp_path, by_command())
    app(work)
    assert "not a valid environment variable" in json.loads(tools.deploy({"path": "bake-sale", "env": {"1X": "v"}}))["error"]
    assert "not a plan question" in json.loads(tools.deploy({"path": "bake-sale", "answers": {"--x": "v"}}))["error"]
    assert fake.calls == []


def test_a_failed_deploy_returns_the_diagnosis(tmp_path):
    failure = ok({"error": {"code": "health_check_failed", "owner": "app", "diagnosis": "crashed"}}, code=1)
    tools, _, work, _, _ = setup(tmp_path, by_command(deploy=failure))
    app(work)
    result = json.loads(tools.deploy({"path": "bake-sale"}))
    assert result["ok"] is False
    assert "who needs to act" in result["summary"]
    assert result["ample_output"]["error"]["owner"] == "app"


def test_an_unchanged_redeploy_says_so_and_offers_the_claim_link_later(tmp_path):
    unchanged = ok({"status": "live", "unchanged": True, "url": "https://s.example", "claim_url": "https://ample.test/claim?token=again"})
    tools, _, work, _, _ = setup(tmp_path, by_command(deploy=unchanged),
                                 secrets=Secrets(AMPLE_CLIENT_ID="agent_9", AMPLE_CLIENT_SECRET="s"))
    tools.account._http.valid_clients.add("agent_9")
    folder = app(work)
    (folder / ".ample").mkdir()
    result = json.loads(tools.deploy({"path": "bake-sale"}))
    assert result["summary"].startswith("No changes since the last deploy. Live at https://s.example")
    assert "claim?token=again" in result["summary"]
    assert "notice" not in result


def test_a_refused_token_is_retried_once_with_a_fresh_one(tmp_path):
    answers = iter([ok({"http_status": 401, "error": {"code": "authentication_failed"}}, code=1), ok(LIVE)])
    tools, fake, work, _, _ = setup(tmp_path, lambda args: next(answers),
                                    secrets=Secrets(AMPLE_CLIENT_ID="cid", AMPLE_CLIENT_SECRET="s"))
    tools.account._http.valid_clients.add("cid")
    folder = app(work)
    (folder / ".ample").mkdir()
    assert json.loads(tools.deploy({"path": "bake-sale"}))["ok"] is True
    assert len(fake.calls) == 2
    token_calls = [c for c in tools.account._http.calls if c[1].endswith("/oauth/token")]
    assert len(token_calls) == 2


def test_a_folder_from_a_deleted_account_deploys_into_the_new_one(tmp_path):
    api, state = FakeApi(), MemoryState()
    mismatch = ok({"error": {"code": "project_account_mismatch"}}, code=1)
    calls = []

    def respond(args):
        calls.append(args)
        if args[2] == "deploy" and len([c for c in calls if c[2] == "deploy"]) == 1:
            return mismatch
        return by_command()(args)

    tools, fake, work, _, secrets = setup(tmp_path, respond, api=api, state=state)
    folder = app(work)
    (folder / ".ample").mkdir()
    (folder / ".ample" / "account.json").write_text(json.dumps({"account_id": "acc_old"}))
    state.data["retired"] = ["acc_old"]

    result = json.loads(tools.deploy({"path": "bake-sale"}))
    assert result["ok"] is True
    assert [c[2] for c in calls].count("deploy") == 2
    # The stale records are gone, so the second deploy planned the folder afresh.
    assert ["plan", str(folder.resolve()), "--offline"] in fake.commands()


def test_a_mismatch_with_an_account_it_never_had_is_left_alone(tmp_path):
    mismatch = ok({"error": {"code": "project_account_mismatch"}}, code=1)
    tools, fake, work, _, _ = setup(tmp_path, by_command(deploy=mismatch))
    folder = app(work)
    (folder / ".ample").mkdir()
    (folder / ".ample" / "account.json").write_text(json.dumps({"account_id": "acc_someone"}))
    result = json.loads(tools.deploy({"path": "bake-sale"}))
    assert result["ok"] is False
    assert (folder / ".ample" / "account.json").exists()
    assert [c[0] for c in fake.commands()].count("deploy") == 1


def test_a_deploy_that_runs_out_of_time_says_to_check_before_retrying(tmp_path):
    slow = cli.CliResult(exit_code=None, stdout="", stderr="", timed_out=True)
    tools, _, work, _, _ = setup(tmp_path, by_command(deploy=slow))
    folder = app(work)
    (folder / ".ample").mkdir()
    result = json.loads(tools.deploy({"path": "bake-sale"}))
    assert result["ok"] is False and "Check ample_apps" in result["error"]


def test_a_suspended_account_is_reported_without_running_the_cli(tmp_path):
    api = FakeApi()
    api.valid_clients.add("cid")
    api.suspended.add("cid")
    tools, fake, work, _, _ = setup(tmp_path, by_command(), api=api,
                                    secrets=Secrets(AMPLE_CLIENT_ID="cid", AMPLE_CLIENT_SECRET="s"))
    app(work)
    result = json.loads(tools.deploy({"path": "bake-sale"}))
    assert "suspended" in result["error"] and "Do not sign up" in result["error"]
    assert fake.calls == []


def test_a_missing_cli_is_reported(tmp_path):
    tools, _, work, _, _ = setup(tmp_path, by_command())

    class Missing:
        def path(self):
            raise cli.CliMissing("Could not download the Ample CLI.")

    tools.binary = Missing()
    app(work)
    assert "Could not download" in json.loads(tools.deploy({"path": "bake-sale"}))["error"]


def test_long_output_keeps_its_head_and_tail(tmp_path):
    noisy = cli.CliResult(exit_code=1, stdout="start " + "x" * 50_000 + " end", stderr="")
    tools, _, work, _, _ = setup(tmp_path, by_command(deploy=noisy))
    folder = app(work)
    (folder / ".ample").mkdir()
    output = json.loads(tools.deploy({"path": "bake-sale"}))["ample_output"]
    assert output.startswith("start ") and output.endswith(" end") and "characters omitted" in output


# -- logs, apps, delete -------------------------------------------------------


def test_nothing_deployed_yet_means_no_logs_apps_or_deletes(tmp_path):
    tools, fake, _, api, _ = setup(tmp_path, by_command())
    assert json.loads(tools.apps({}))["summary"].startswith("No apps yet")
    assert "no logs" in json.loads(tools.logs({"deployment_id": "dep_1"}))["error"]
    assert "nothing to delete" in json.loads(tools.delete({"deployment_id": "dep_1"}))["error"]
    assert fake.calls == [] and api.signups == 0


def test_logs_apps_and_delete_run_the_cli(tmp_path):
    tools, fake, _, api, _ = setup(tmp_path, by_command(logs=ok({"lines": ["ready"]}), app=ok([{"id": "dep_1", "name": "site"}])),
                                   secrets=Secrets(AMPLE_CLIENT_ID="cid", AMPLE_CLIENT_SECRET="s"))
    api.valid_clients.add("cid")
    assert json.loads(tools.logs({"deployment_id": "dep_1", "kind": "runtime", "tail": 20}))["ok"] is True
    listed = json.loads(tools.apps({}))
    assert listed["summary"] == "Apps:" and listed["ample_output"] == [{"id": "dep_1", "name": "site"}]
    assert json.loads(tools.delete({"deployment_id": "dep_1"}))["summary"].startswith("Deleted.")
    assert fake.commands() == [
        ["logs", "dep_1", "--kind", "runtime", "--tail", "20"],
        ["app", "list"],
        ["app", "delete", "dep_1", "--yes"],
    ]


@pytest.mark.parametrize("args, reason", [
    ({"deployment_id": "--all"}, "not a deployment ID"),
    ({"deployment_id": "dep_1", "kind": "secrets"}, "kind is"),
    ({"deployment_id": "dep_1", "tail": 0}, "tail is"),
    ({"deployment_id": "dep_1", "tail": True}, "tail is"),
])
def test_bad_log_arguments_are_refused(tmp_path, args, reason):
    tools, fake, _, _, _ = setup(tmp_path, by_command())
    assert reason in json.loads(tools.logs(args))["error"]
    assert fake.calls == []
