"""Fakes for Ample's API, the CLI and Hermes's plugin state."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from hermes_ample import cli
from hermes_ample.account import Account, Settings


class MemoryState:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class FakeApi:
    """Ample's signup, token and claim endpoints."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Optional[bytes]]] = []
        self.signups = 0
        self.valid_clients: set[str] = set()
        self.suspended: set[str] = set()
        self.signup_status = 201
        self.signup_keys: list[str] = []

    def __call__(self, method: str, url: str, headers: dict, body: Optional[bytes], timeout: float) -> tuple[int, bytes]:
        self.calls.append((method, url, body))
        path = url.split("ample.test", 1)[1]
        if path == "/v1/auth/signup":
            key = json.loads(body)["request_key"]
            self.signup_keys.append(key)
            if self.signup_status != 201:
                return self.signup_status, b'{"error":"busy"}'
            self.signups += 1
            client = f"agent_{self.signups}"
            self.valid_clients.add(client)
            return 201, json.dumps({
                "account_id": f"acc_{self.signups}",
                "credentials": {"client_id": client, "client_secret": f"secret_{self.signups}"},
                "claim_url": f"https://ample.test/claim?token=c{self.signups}",
                "account_expires_at": "2026-10-13T00:00:00Z",
                "access_token": f"onboarding_{self.signups}",
            }).encode()
        if path == "/oauth/token":
            form = dict(pair.split("=", 1) for pair in body.decode().split("&"))
            client = form["client_id"]
            if client in self.suspended:
                return 403, b'{"error":"account_suspended","error_description":"suspended"}'
            if client not in self.valid_clients:
                return 401, b'{"error":"invalid_client"}'
            return 200, json.dumps({"access_token": f"token_for_{client}", "expires_in": 900}).encode()
        if path == "/v1/auth/claim":
            return 200, b'{"email_sent":true}'
        return 404, b"{}"


class Secrets:
    def __init__(self, **values: str) -> None:
        self.values = dict(values)
        self.fail_saves = False

    def get(self, name: str) -> Optional[str]:
        return self.values.get(name)

    def save(self, name: str, value: str) -> None:
        if self.fail_saves:
            raise PermissionError("read-only .env")
        self.values[name] = value


def make_account(api: FakeApi, secrets: Secrets, state: Optional[MemoryState] = None,
                 now: Callable[[], float] = lambda: 1_000_000.0, **settings: Any) -> Account:
    return Account(
        Settings(api_url="https://ample.test", **settings),
        secret=secrets.get,
        save_secret=secrets.save,
        state=state or MemoryState(),
        http=api,
        now=now,
    )


class FakeCli:
    """Answers CLI commands from a script: a list of (predicate, CliResult) or a function."""

    def __init__(self, respond: Callable[[list[str]], cli.CliResult]) -> None:
        self.respond = respond
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, binary, args: list[str], *, env: dict, timeout: float) -> cli.CliResult:
        self.calls.append((list(args), dict(env)))
        return self.respond(list(args))

    def commands(self) -> list[list[str]]:
        """The CLI arguments of each call, without the leading --format json."""
        return [args[2:] for args, _ in self.calls]


def ok(value: Any, code: int = 0) -> cli.CliResult:
    return cli.CliResult(exit_code=code, stdout=json.dumps(value), stderr="")
