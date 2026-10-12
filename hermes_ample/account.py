"""Ample credentials and the account Hermes deploys into.

A credential is either a plain access token (``AMPLE_TOKEN``) or an agent
credential (``AMPLE_CLIENT_ID`` + ``AMPLE_CLIENT_SECRET``) that is exchanged for
a short-lived access token before each command. With neither set, and signup
on, the plugin signs up for a free Ample account on the first deploy and saves
its credential in Hermes's ``.env``. The owner keeps that account by claiming it
(confirming their email); Ample deletes an unclaimed account two days after
signup, and the next deploy then signs up again.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from . import __version__

DEFAULT_API_URL = "https://api.ample.computer"
USER_AGENT = f"hermes-ample/{__version__}"

TOKEN = "AMPLE_TOKEN"
CLIENT_ID = "AMPLE_CLIENT_ID"
CLIENT_SECRET = "AMPLE_CLIENT_SECRET"

# Renew this long before the token's stated expiry, so a long deploy never starts on a stale one.
RENEW_MARGIN_SECS = 60
# Ample keeps a signup receipt for ten minutes; reuse the key a little less long than that.
RECEIPT_REUSE_SECS = 9 * 60

SUSPENDED = (
    "Ample has suspended this account, so nothing can be deployed or changed. Tell the owner to "
    "contact Ample at https://ample.computer/contact. Do not sign up for another account."
)


class AmpleError(Exception):
    """A failure the model can read and relay. Never carries a credential."""


class InvalidClient(AmpleError):
    """Ample no longer knows the client: its account was deleted or the credential revoked."""


class AccountSuspended(AmpleError):
    """Ample suspended the account. Signing up again would work around it, so the agent stops."""


class State(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...


# (method, url, headers, body, timeout) -> (status, body). Raises OSError on a network failure.
Http = Callable[[str, str, dict, Optional[bytes], float], "tuple[int, bytes]"]


def urllib_http(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


@dataclass
class Issued:
    token: str
    # Something the owner should hear about, such as a new account and its claim link.
    notice: str = ""


@dataclass
class Settings:
    api_url: str = DEFAULT_API_URL
    # Sign up for an account of the owner's own when no credential is set.
    signup: bool = True
    # The owner's email: Ample sends them a claim link right after signup.
    owner_email: str = ""


class Account:
    """The credential for this Hermes profile, signing up first when allowed and needed."""

    def __init__(
        self,
        settings: Settings,
        *,
        secret: Callable[[str], Optional[str]],
        save_secret: Callable[[str, str], None],
        state: State,
        http: Http = urllib_http,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._secret = secret
        self._save_secret = save_secret
        self._state = state
        self._http = http
        self._now = now
        self._lock = threading.Lock()
        self._cached: Optional[tuple[str, str, float]] = None  # client_id, token, expires_at
        # A credential this process signed up for, in case saving it to .env failed.
        self._fresh: Optional[tuple[str, str]] = None

    @property
    def api_url(self) -> str:
        return self.settings.api_url.rstrip("/")

    def has_account(self) -> bool:
        """Whether there is an account to act on yet. Only before the first signup is there none."""
        return bool(self._secret(TOKEN)) or self._client() is not None

    def retired(self, account_id: str) -> bool:
        """Whether ``account_id`` was this profile's account before it had to sign up again."""
        return account_id in (self._state.get("retired") or [])

    def claim_url(self) -> str:
        """Where the owner keeps the account Hermes signed up for, while it is unclaimed."""
        account = self._state.get("account") or {}
        return account.get("claim_url") or ""

    def drop_token(self) -> None:
        """Ample refused the current access token (its account may be gone): fetch a new one next time."""
        self._cached = None

    def token(self) -> Issued:
        plain = self._secret(TOKEN)
        if plain:
            return Issued(plain)
        client = self._client()
        if client is None:
            if not self.settings.signup:
                raise AmpleError(
                    "No Ample account is set up. Set AMPLE_CLIENT_ID and AMPLE_CLIENT_SECRET (or AMPLE_TOKEN) "
                    "in Hermes, or turn the plugin's signup setting back on."
                )
            with self._lock:
                client = self._client()
                notice = ""
                if client is None:
                    client, notice = self._signup()
            return Issued(self._exchange(*client), notice)
        try:
            return Issued(self._exchange(*client))
        except InvalidClient:
            if not self._owns(client[0]):
                raise AmpleError(
                    "Ample does not accept the configured credential (it was revoked, or its account deleted). "
                    "Check AMPLE_CLIENT_ID and AMPLE_CLIENT_SECRET."
                ) from None
            # Ample deletes an account nobody claimed within two days. Start a new one.
            with self._lock:
                current = self._client()
                notice = ""
                if current == client:
                    self._retire()
                    current, notice = self._signup()
                    lost = (
                        "The owner's previous Ample account was deleted because nobody claimed it within two "
                        "days, so the sites in it are gone."
                    )
                    notice = f"{lost} {notice}".strip()
            return Issued(self._exchange(*current), notice)

    def _client(self) -> Optional[tuple[str, str]]:
        client_id, client_secret = self._secret(CLIENT_ID), self._secret(CLIENT_SECRET)
        if client_id and client_secret:
            return client_id, client_secret
        return self._fresh

    def _owns(self, client_id: str) -> bool:
        """Whether the plugin signed up for this credential (so it may replace it)."""
        if (self._state.get("account") or {}).get("client_id") == client_id:
            return True
        # Still in .env after its account was deleted, when the signup to replace it failed.
        return client_id in (self._state.get("retired_clients") or [])

    def _retire(self) -> None:
        account = self._state.get("account") or {}
        if account.get("account_id"):
            self._state.set("retired", [*(self._state.get("retired") or []), account["account_id"]][-50:])
        if account.get("client_id"):
            self._state.set("retired_clients", [*(self._state.get("retired_clients") or []), account["client_id"]][-50:])
        self._state.set("account", None)
        self._fresh = None
        self._cached = None

    def _exchange(self, client_id: str, client_secret: str) -> str:
        cached = self._cached
        if cached and cached[0] == client_id and cached[2] - RENEW_MARGIN_SECS > self._now():
            return cached[1]
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "resource": f"{self.api_url}/mcp",
            }
        ).encode()
        status, raw = self._request(
            "POST", "/oauth/token", body, {"Content-Type": "application/x-www-form-urlencoded"}, 30, "sign-in"
        )
        # Never echo the response body: an auth error is not worth risking the secret.
        if status != 200:
            code = _error_code(raw) if status in (401, 403) else None
            if code == "invalid_client":
                raise InvalidClient(f"Ample sign-in failed with HTTP {status}.")
            if code == "account_suspended":
                raise AccountSuspended(SUSPENDED)
            raise AmpleError(f"Ample sign-in failed with HTTP {status}.")
        value = _json(raw)
        token, ttl = value.get("access_token"), value.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(ttl, (int, float)) or ttl <= 0:
            raise AmpleError("Ample sign-in returned no access token.")
        self._cached = (client_id, token, self._now() + ttl)
        return token

    def _signup(self) -> tuple[tuple[str, str], str]:
        pending = self._state.get("pending") or {}
        if not (pending.get("request_key") and self._now() - pending.get("started_at", 0) < RECEIPT_REUSE_SECS):
            pending = {"request_key": str(uuid.uuid4()), "started_at": self._now()}
        # Saved before the request, so a crash or timeout retries with the same key and gets the same account.
        self._state.set("pending", pending)
        body = json.dumps({"name": "hermes", "request_key": pending["request_key"]}).encode()
        status, raw = self._request("POST", "/v1/auth/signup", body, {"Content-Type": "application/json"}, 130, "signup")
        # The body holds credentials on success only; an error body is never echoed.
        if status not in (200, 201):
            raise AmpleError(f"Ample signup failed with HTTP {status}.")
        value = _json(raw)
        credentials = value.get("credentials") or {}
        client_id, client_secret = credentials.get("client_id"), credentials.get("client_secret")
        account_id = value.get("account_id")
        if not all(isinstance(v, str) and v for v in (client_id, client_secret, account_id)):
            raise AmpleError("Ample signup returned no credentials.")

        account = {"account_id": account_id, "client_id": client_id, "created_at": self._now()}
        if isinstance(value.get("claim_url"), str):
            account["claim_url"] = value["claim_url"]
        if isinstance(value.get("account_expires_at"), str):
            account["expires_at"] = value["account_expires_at"]
        email = self.settings.owner_email.strip()
        if email and isinstance(value.get("access_token"), str) and self._email_claim_link(value["access_token"], email):
            account["claim_emailed_to"] = email

        self._fresh = (client_id, client_secret)
        saved = ""
        try:
            self._save_secret(CLIENT_ID, client_id)
            self._save_secret(CLIENT_SECRET, client_secret)
        except Exception as error:  # noqa: BLE001 - reported to the owner, never fatal
            saved = (
                f" Hermes could not save the new credential ({type(error).__name__}), so it lasts only until "
                "Hermes restarts."
            )
        self._state.set("account", account)
        self._state.set("pending", None)

        keep = ""
        if account.get("claim_url"):
            keep = f" Send the owner this link once so they keep their sites: {account['claim_url']} (they confirm their email there)."
        mailed = ""
        if account.get("claim_emailed_to"):
            mailed = f" Ample also emailed a claim link to {email}; it works for 30 minutes."
        notice = (
            "Created a free Ample account for the owner's sites. It is deleted two days after signup unless "
            f"the owner claims it.{keep}{mailed}{saved}"
        )
        return (client_id, client_secret), notice

    def _email_claim_link(self, token: str, email: str) -> bool:
        try:
            status, raw = self._request(
                "POST",
                "/v1/auth/claim",
                json.dumps({"email": email}).encode(),
                {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                30,
                "claim",
            )
        except AmpleError:
            return False
        # The claim link in the deploy result still works; an email is a convenience.
        return status == 200 and _json(raw).get("email_sent") is True

    def _request(self, method: str, path: str, body: bytes, headers: dict, timeout: float, what: str) -> tuple[int, bytes]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
        try:
            return self._http(method, f"{self.api_url}{path}", headers, body, timeout)
        except OSError as error:
            raise AmpleError(f"Could not reach Ample for {what}: {type(error).__name__}.") from None


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _error_code(raw: bytes) -> Optional[str]:
    """The OAuth error code only (``{"error": "invalid_client"}``); the description is never read."""
    code = _json(raw).get("error")
    return code if isinstance(code, str) else None
