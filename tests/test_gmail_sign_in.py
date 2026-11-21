"""PRD R19: the Gmail sign-in path was the least-tested code (sync.py 74%) and
the code that actually failed in production (invalid_grant every hour, Nov 6-12).
Google's classes are faked; nothing here touches a real account."""

import json
import os
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import jt
import notify
import sync


class FakeCreds:
    def __init__(self, valid=True, expired=False, refresh_token="r", refresh_error=None, label="t"):
        self.valid, self.expired, self.refresh_token = valid, expired, refresh_token
        self.refresh_error, self.label, self.refreshed = refresh_error, label, False

    def refresh(self, request):
        if self.refresh_error:
            raise self.refresh_error
        self.refreshed, self.valid, self.expired = True, True, False

    def to_json(self):
        return json.dumps({"token": self.label, "refreshed": self.refreshed})


class WSGITimeout(Exception):
    pass


@pytest.fixture
def google(tmp_path, monkeypatch):
    """Fake Google client classes. Configure .stored (token on disk) and
    .browser (what the sign-in flow returns or raises)."""
    state = type("G", (), {})()
    state.stored = None
    state.browser = FakeCreds(label="fresh")
    state.browser_opened = False
    state.built_with = None

    class Credentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            return state.stored

    class Flow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            return Flow()

        def run_local_server(self, port, timeout_seconds):
            state.browser_opened = True
            if isinstance(state.browser, Exception):
                raise state.browser
            return state.browser

    def build(name, version, credentials):
        state.built_with = credentials
        return "service"

    monkeypatch.setattr(sync, "_require_google_clients",
                        lambda: (lambda: "request", Credentials, Flow, WSGITimeout, build))
    client = tmp_path / "client.json"
    client.write_text("{}")
    token = tmp_path / "token.json"
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT", str(client))
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))
    monkeypatch.delenv("JT_UNATTENDED", raising=False)
    state.token = token
    return state


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------- setup errors

def test_missing_oauth_client_file_is_a_clear_error(google, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT", str(tmp_path / "nope.json"))
    with pytest.raises(SystemExit, match="GOOGLE_OAUTH_CLIENT does not exist"):
        sync.build_gmail_service()


def test_unattended_without_a_token_never_opens_a_browser(google, monkeypatch):
    monkeypatch.setenv("JT_UNATTENDED", "1")
    with pytest.raises(SystemExit, match="run jt-sync once interactively"):
        sync.build_gmail_service()
    assert not google.browser_opened


# ---------------------------------------------------------------- normal use

def test_valid_token_is_used_as_is_and_locked_down(google):
    google.token.write_text("old")
    os.chmod(google.token, 0o644)
    google.stored = FakeCreds(valid=True, label="stored")
    assert sync.build_gmail_service() == "service"
    assert google.built_with.label == "stored"
    assert mode(google.token) == 0o600          # loose permissions are tightened
    assert google.token.read_text() == "old"    # a valid token isn't rewritten


def test_expired_token_is_refreshed_and_saved_privately(google):
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True)
    sync.build_gmail_service()
    assert json.loads(google.token.read_text())["refreshed"] is True
    assert mode(google.token) == 0o600
    assert not google.browser_opened


def test_first_sign_in_opens_the_browser_and_saves_the_token(google):
    sync.build_gmail_service()
    assert google.browser_opened
    assert json.loads(google.token.read_text())["token"] == "fresh"
    assert mode(google.token) == 0o600


def test_sign_in_timeout_explains_what_to_check(google):
    google.browser = WSGITimeout("timed out")
    with pytest.raises(SystemExit, match="timed out. Confirm the Google Cloud"):
        sync.build_gmail_service()
    assert not google.token.exists()


# ---------------------------------------------------------------- the production failure

class RefreshError(Exception):
    pass


def test_revoked_token_unattended_fails_with_an_auth_message(google, monkeypatch):
    """The Nov 6-12 outage: the hourly job hit invalid_grant every run."""
    monkeypatch.setenv("JT_UNATTENDED", "1")
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True,
                              refresh_error=RefreshError("invalid_grant: Token has been expired or revoked."))
    with pytest.raises(SystemExit, match="invalid_grant"):
        sync.build_gmail_service()
    assert not google.browser_opened
    assert google.token.read_text() == "old"     # nothing overwritten on failure


def test_revoked_token_interactive_signs_in_again_and_saves_the_new_token(google):
    """Running jt-sync in Terminal after the token dies must recover on its own,
    without moving token.json aside by hand (the Nov 21 manual workaround)."""
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True,
                              refresh_error=RefreshError("invalid_grant: Token has been expired or revoked."))
    assert sync.build_gmail_service() == "service"
    assert google.browser_opened
    assert google.built_with.label == "fresh"
    assert json.loads(google.token.read_text())["token"] == "fresh"
    assert mode(google.token) == 0o600


def test_revoked_token_interactive_keeps_the_old_token_if_sign_in_times_out(google):
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True,
                              refresh_error=RefreshError("invalid_grant: Token has been expired or revoked."))
    google.browser = WSGITimeout("timed out")
    with pytest.raises(SystemExit, match="timed out"):
        sync.build_gmail_service()
    assert google.token.read_text() == "old"     # never deleted before a new one is saved


def test_revoked_token_in_audit_mode_fails_without_a_browser(google):
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True,
                              refresh_error=RefreshError("invalid_grant: Token has been expired or revoked."))
    with pytest.raises(SystemExit, match="invalid_grant"):
        sync.build_gmail_service(persist_credentials=False)
    assert not google.browser_opened
    assert google.token.read_text() == "old"


def test_other_refresh_errors_are_not_mislabelled_as_auth(google):
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True, refresh_error=RefreshError("connection reset"))
    with pytest.raises(RefreshError, match="connection reset"):
        sync.build_gmail_service()


def test_revoked_token_reaches_you_as_a_sign_in_alert(google, tmp_path, monkeypatch):
    """End to end: sync.main fails on invalid_grant, logs it, jt reads it as an
    expired sign-in, and the notification tells you how to fix it."""
    monkeypatch.setenv("JT_UNATTENDED", "1")
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(sync, "LOG_PATH", tmp_path / "logs" / "jt-sync.jsonl")
    monkeypatch.setattr("sys.argv", ["sync.py"])
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True,
                              refresh_error=RefreshError("invalid_grant: Token has been expired or revoked."))
    with pytest.raises(SystemExit):
        sync.main()
    assert jt.sync_error_state() == "auth_expired"
    conn = jt.connect()
    [alert] = notify.notification_plan(conn, datetime(2026, 11, 14, 9, tzinfo=ZoneInfo("America/New_York")))
    conn.close()
    assert alert["title"] == "jt can't read your Gmail"
    assert "sign-in expired" in alert["message"]


# ---------------------------------------------------------------- audit mode

def test_audit_mode_never_writes_or_opens_a_browser(google):
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True)
    sync.build_gmail_service(persist_credentials=False)
    assert google.token.read_text() == "old"
    google.stored = None
    with pytest.raises(SystemExit, match="not valid for unattended use"):
        sync.build_gmail_service(persist_credentials=False)
    assert not google.browser_opened


def test_unattended_with_an_unrefreshable_token_never_opens_a_browser(google, monkeypatch):
    """Found by mutation testing: the hourly job must never pop a sign-in window,
    even when a token file exists but can't be refreshed (no refresh token)."""
    monkeypatch.setenv("JT_UNATTENDED", "1")
    google.token.write_text("old")
    google.stored = FakeCreds(valid=False, expired=True, refresh_token=None)
    with pytest.raises(SystemExit, match="not valid for unattended use"):
        sync.build_gmail_service()
    assert not google.browser_opened
