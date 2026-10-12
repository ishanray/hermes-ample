import threading

import pytest

from hermes_ample.account import AccountSuspended, AmpleError
from tests.helpers import FakeApi, MemoryState, Secrets, make_account


def test_a_plain_token_is_used_as_is():
    api = FakeApi()
    issued = make_account(api, Secrets(AMPLE_TOKEN="tok_plain")).token()
    assert issued.token == "tok_plain"
    assert api.calls == []


def test_a_client_credential_is_exchanged_once_and_cached():
    api = FakeApi()
    api.valid_clients.add("cid")
    account = make_account(api, Secrets(AMPLE_CLIENT_ID="cid", AMPLE_CLIENT_SECRET="cs"))
    assert account.token().token == "token_for_cid"
    assert account.token().token == "token_for_cid"
    assert [url for _, url, _ in api.calls] == ["https://ample.test/oauth/token"]


def test_the_first_call_signs_up_and_saves_the_credential_in_hermes():
    api, secrets, state = FakeApi(), Secrets(), MemoryState()
    account = make_account(api, secrets, state)
    assert not account.has_account()

    first = account.token()
    assert first.token == "token_for_agent_1"
    assert "https://ample.test/claim?token=c1" in first.notice
    assert "deleted two days after signup" in first.notice
    assert secrets.values == {"AMPLE_CLIENT_ID": "agent_1", "AMPLE_CLIENT_SECRET": "secret_1"}
    # No secret in plugin state: only what identifies the account.
    assert state.data["account"]["client_id"] == "agent_1"
    assert "secret_1" not in repr(state.data)

    second = account.token()
    assert second.notice == ""
    assert api.signups == 1
    assert account.has_account()


def test_signup_off_without_a_credential_is_an_error():
    with pytest.raises(AmpleError, match="AMPLE_CLIENT_ID"):
        make_account(FakeApi(), Secrets(), signup=False).token()


def test_a_failed_signup_retries_with_the_same_request_key():
    api, state = FakeApi(), MemoryState()
    api.signup_status = 503
    account = make_account(api, Secrets(), state)
    with pytest.raises(AmpleError, match="HTTP 503"):
        account.token()
    api.signup_status = 201
    account.token()
    assert len(api.signup_keys) == 2 and api.signup_keys[0] == api.signup_keys[1]


def test_an_old_request_key_is_not_reused():
    api, state = FakeApi(), MemoryState()
    clock = [1_000_000.0]
    api.signup_status = 503
    account = make_account(api, Secrets(), state, now=lambda: clock[0])
    with pytest.raises(AmpleError):
        account.token()
    clock[0] += 10 * 60
    api.signup_status = 201
    account.token()
    assert api.signup_keys[0] != api.signup_keys[1]


def test_a_deleted_account_it_signed_up_for_is_replaced_and_the_owner_told():
    api, secrets, state = FakeApi(), Secrets(), MemoryState()
    account = make_account(api, secrets, state)
    account.token()
    # Ample deletes the unclaimed account.
    api.valid_clients.discard("agent_1")
    account.drop_token()

    issued = account.token()
    assert issued.token == "token_for_agent_2"
    assert "previous Ample account was deleted" in issued.notice
    assert "claim?token=c2" in issued.notice
    assert account.retired("acc_1")
    assert secrets.values["AMPLE_CLIENT_ID"] == "agent_2"


def test_a_failed_replacement_signup_is_retried_on_the_next_call():
    api, secrets, state = FakeApi(), Secrets(), MemoryState()
    account = make_account(api, secrets, state)
    account.token()
    api.valid_clients.discard("agent_1")
    account.drop_token()
    api.signup_status = 503
    with pytest.raises(AmpleError, match="HTTP 503"):
        account.token()
    # The old credential is still in .env; it is still the plugin's to replace.
    api.signup_status = 201
    issued = account.token()
    assert issued.token == "token_for_agent_2"
    assert secrets.values["AMPLE_CLIENT_ID"] == "agent_2"


def test_a_credential_the_owner_set_is_never_replaced():
    api = FakeApi()
    account = make_account(api, Secrets(AMPLE_CLIENT_ID="theirs", AMPLE_CLIENT_SECRET="s"))
    with pytest.raises(AmpleError, match="Check AMPLE_CLIENT_ID"):
        account.token()
    assert api.signups == 0


def test_a_suspended_account_stops_instead_of_signing_up_again():
    api, secrets, state = FakeApi(), Secrets(), MemoryState()
    account = make_account(api, secrets, state)
    account.token()
    api.suspended.add("agent_1")
    account.drop_token()
    with pytest.raises(AccountSuspended, match="Do not sign up for another account"):
        account.token()
    assert api.signups == 1


def test_a_credential_hermes_could_not_save_still_works_until_restart():
    api, secrets = FakeApi(), Secrets()
    secrets.fail_saves = True
    account = make_account(api, secrets)
    issued = account.token()
    assert "lasts only until Hermes restarts" in issued.notice
    assert account.token().token == "token_for_agent_1"
    assert api.signups == 1


def test_the_owner_email_gets_a_claim_link_at_signup():
    api = FakeApi()
    issued = make_account(api, Secrets(), owner_email="owner@example.com").token()
    assert "emailed a claim link to owner@example.com" in issued.notice
    assert any(url.endswith("/v1/auth/claim") for _, url, _ in api.calls)


def test_concurrent_first_deploys_share_one_signup():
    api = FakeApi()
    account = make_account(api, Secrets())
    tokens = []
    threads = [threading.Thread(target=lambda: tokens.append(account.token().token)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert api.signups == 1
    assert set(tokens) == {"token_for_agent_1"}


def test_an_unreachable_api_is_a_readable_error():
    def offline(*_):
        raise ConnectionRefusedError("refused")

    account = make_account(offline, Secrets(AMPLE_CLIENT_ID="c", AMPLE_CLIENT_SECRET="s"))
    with pytest.raises(AmpleError, match="Could not reach Ample"):
        account.token()
