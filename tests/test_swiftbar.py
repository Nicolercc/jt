"""SwiftBar panel (M5 prototype). It must render every state of the D5
contract, never leak company names into the menu-bar title, and log usage."""

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "scripts/swiftbar/jt.5m.py"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_USAGE_LOG", str(tmp_path / "usage.log"))
    monkeypatch.setenv("JT_PYTHON", sys.executable)
    monkeypatch.setenv("JT_REPO", str(ROOT))
    spec = importlib.util.spec_from_file_location("jt_swiftbar", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def snap(**over):
    s = {
        "schema_version": 1, "generated_at": "2026-08-05T16:00:00Z",
        "badge": {"due": 0, "reviews": 0},
        "freshness": {"gmail_synced_at": "2026-08-05T15:48:00Z",
                      "classified_at": "2026-08-05T15:49:00Z", "sync_error": None},
        "actions": [], "suggestions": [],
        "review": {"pending": 0, "oldest_days": 0, "top": None},
    }
    s.update(over)
    return s


def act(i, desc="Send availability", company="Acme", overdue=False, url=None, reason=None):
    return {"action_id": i, "application_id": 1, "company": company, "role": "Engineer",
            "description": desc, "due": {"kind": "date", "value": "2026-08-05"},
            "overdue": overdue, "origin": "manual", "reason": reason,
            "gmail_msg_id": None, "gmail_url": url}


# ---------------------------------------------------------------- render

def test_empty_state(plugin):
    lines = plugin.render(snap(), None, NOW)
    assert lines[0] == "jt"
    assert any("Nothing due" in x for x in lines)
    assert any("Gmail synced 12 min ago" in x for x in lines)


def test_title_counts_but_never_names_companies(plugin):
    s = snap(badge={"due": 2, "reviews": 1},
             actions=[act(1, company="Secret Employer"), act(2, company="Other Co")])
    title = plugin.render(s, None, NOW)[0]
    assert title == "jt 2 · 1?"
    assert "Secret" not in title and "Other" not in title


def test_shows_at_most_three_actions_with_done_and_snooze(plugin):
    s = snap(badge={"due": 5, "reviews": 0}, actions=[act(i) for i in range(1, 6)])
    lines = plugin.render(s, None, NOW)
    assert sum(1 for x in lines if x.startswith("--Done")) == 3
    assert sum(1 for x in lines if x.startswith("--Snooze")) == 3
    assert any("+2 more" in x for x in lines)
    done = next(x for x in lines if x.startswith("--Done"))
    assert 'param1=done param2="1"' in done and "refresh=true" in done


def test_open_email_only_when_the_action_came_from_an_email(plugin):
    url = "https://mail.google.com/mail/u/0/#all/18cabc"
    lines = plugin.render(snap(badge={"due": 2, "reviews": 0},
                               actions=[act(1, url=url), act(2)]), None, NOW)
    opens = [x for x in lines if x.startswith("--Open email")]
    assert len(opens) == 1 and f'param2="{url}"' in opens[0]


def test_overdue_and_reason_are_visible(plugin):
    lines = plugin.render(snap(badge={"due": 1, "reviews": 0},
                               actions=[act(1, overdue=True, reason="Recruiter asked")]), None, NOW)
    assert any("overdue" in x for x in lines)
    assert any("Why: Recruiter asked" in x for x in lines)


@pytest.mark.parametrize("err,text", [
    ("offline", "Offline"), ("auth_expired", "sign-in expired"), ("failed", "sync failed")])
def test_sync_problems_are_shown_in_words_and_marked_in_title(plugin, err, text):
    s = snap(freshness={"gmail_synced_at": None, "classified_at": None, "sync_error": err})
    lines = plugin.render(s, None, NOW)
    assert lines[0].endswith("⚠")
    assert any(text in x for x in lines)


def test_review_count_and_oldest_reason(plugin):
    s = snap(badge={"due": 0, "reviews": 2},
             review={"pending": 2, "oldest_days": 1,
                     "top": {"review_id": 4, "reason": "needs your confirmation: rejected for #3"}})
    lines = plugin.render(s, None, NOW)
    assert any(x.startswith("2 to review") for x in lines)
    assert any("needs your confirmation" in x for x in lines)


def test_pipes_and_newlines_cannot_break_the_menu(plugin):
    s = snap(badge={"due": 1, "reviews": 0},
             actions=[act(1, desc="Reply | today\nplease", company="A|B")])
    lines = plugin.render(s, None, NOW)
    assert any(x.startswith("Reply / today please |") for x in lines)
    assert all("\n" not in x for x in lines)


def test_jt_failure_shows_an_error_menu(plugin):
    lines = plugin.render(None, "error: no such table", NOW)
    assert lines[0] == "jt ⚠" and any("no such table" in x for x in lines)


# ---------------------------------------------------------------- end to end

def test_reads_a_real_jt_snapshot(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    s, err = plugin.load_snapshot()
    assert err is None and s["schema_version"] == 1


def test_unknown_schema_version_is_refused(plugin, monkeypatch):
    class R:
        returncode, stdout, stderr = 0, json.dumps({"schema_version": 2}), ""
    monkeypatch.setattr(plugin, "jt", lambda *a, **k: R())
    s, err = plugin.load_snapshot()
    assert s is None and "schema_version" in err


def test_done_click_marks_done_and_logs(plugin, tmp_path, monkeypatch):
    import jt as jtmod
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("JT_TZ", "America/New_York")
    c = jtmod.connect()
    with jtmod.write_txn(c):
        c.execute("INSERT INTO applications (company, role, lane, applied_on) "
                  "VALUES ('Acme', 'Engineer', 'swe', '2026-08-01')")
    x = jtmod.add_action(c, 1, "Send availability", due_date="2026-08-05")
    assert plugin.handle_click(["done", str(x)]) == 0
    assert c.execute("SELECT state FROM actions").fetchone()[0] == "done"
    c.close()
    events = [json.loads(line) for line in (tmp_path / "usage.log").read_text().splitlines()]
    assert events[-1]["event"] == "click" and events[-1]["action"] == "done"


def test_failed_click_is_logged(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    assert plugin.handle_click(["done", "999"]) == 1
    events = [json.loads(line) for line in (tmp_path / "usage.log").read_text().splitlines()]
    assert events[-1]["event"] == "click_failed"


def test_menu_open_is_logged(plugin, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("SWIFTBAR_PLUGIN_REFRESH_REASON", "MenuOpen")
    monkeypatch.setattr(sys, "argv", ["jt.5m.py"])
    plugin.main()
    assert capsys.readouterr().out.startswith("jt")
    assert '"event": "open"' in (tmp_path / "usage.log").read_text()


def test_snooze_goes_to_tomorrow_morning(plugin):
    assert plugin.tomorrow_at("09:00", date(2026, 8, 5)) == "2026-08-06 09:00"


def test_cached_rows_have_context_without_mutation_controls(plugin):
    lines = plugin.render(snap(actions=[act(1)]), "offline", NOW)
    assert any("Showing data from" in line for line in lines)
    assert any("Acme · Engineer · Today" in line for line in lines)
    assert not any(line.startswith(("--Done", "--Snooze")) for line in lines)
    assert any("Quit jt panel" in line for line in lines)


def test_done_retry_recognizes_already_completed(plugin, tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.setattr(plugin, "CACHE", tmp_path / "panel-cache.json")
    plugin.save_cache(snap(actions=[act(1)]))
    replies = iter([
        CompletedProcess([], 1, "", ""),
        CompletedProcess([], 1, '{"ok":false,"error":"action #1 is already done"}', ""),
    ])
    monkeypatch.setattr(plugin, "jt", lambda *args: next(replies))
    assert plugin.handle_click(["done", "1"]) == 1
    assert any("Retry" in line for line in plugin.render(plugin.read_cache(), None, NOW))
    assert plugin.handle_click(["done", "1"]) == 0
    assert "failure" not in plugin.read_cache()


def test_review_launcher_quotes_paths_with_spaces(plugin, monkeypatch):
    import shlex
    from subprocess import CompletedProcess

    monkeypatch.setattr(plugin, "JT_REPO", "/tmp/Repo with spaces")
    monkeypatch.setattr(plugin, "JT_PYTHON", "/tmp/Python with spaces")
    def launch(args):
        assert args[:3] == ["open", "-a", "Terminal"]
        path = Path(args[3])
        assert shlex.split(path.read_text().splitlines()[-1]) == [
            "exec", "/tmp/Python with spaces", "/tmp/Repo with spaces/jt.py", "review",
        ]
        return CompletedProcess(args, 1)
    monkeypatch.setattr(plugin.subprocess, "run", launch)
    assert plugin.launch_review() == 1


def test_suggestion_remind_and_dismiss_are_commands(plugin):
    lines = plugin.render(snap(suggestions=[{
        "kind": "FOLLOW UP", "company": "Acme", "role": "Engineer",
        "application_id": 7, "reason": "No response",
    }]), None, NOW)
    assert any('param1=remind param2="7" param3="FOLLOW UP" param4="2026-08-06"' in row for row in lines)
    assert any('param1=dismiss-suggestion param2="7"' in row for row in lines)


def test_timed_action_formats_role_and_day(plugin):
    action = act(1)
    action["due"] = {"kind": "time", "value": "2026-08-05T15:00:00Z"}
    assert any("Acme · Engineer · Today 3 PM" in row
               for row in plugin.render(snap(actions=[action]), None, NOW))


def test_quit_does_not_use_applescript(plugin, monkeypatch):
    """AppleScript needs the 'control another app' permission, which users
    reasonably decline; Quit must work without it."""
    calls = []
    monkeypatch.setattr(plugin.subprocess, "run",
                        lambda args, **k: calls.append(args) or type("R", (), {"returncode": 0})())
    assert plugin.handle_click(["quit"]) == 0
    assert calls and calls[0][0] != "osascript"
    assert "osascript" not in PLUGIN.read_text()
