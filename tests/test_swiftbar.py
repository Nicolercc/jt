"""SwiftBar panel (M5 prototype). It must render every state of the D5
contract, never leak company names into the menu-bar title, and log usage."""

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "scripts/swiftbar/jt.5m.py"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_USAGE_LOG", str(tmp_path / "usage.log"))
    # Never touch the real ~/.jobtrack/panel-cache.json from tests: a test once
    # left a fake "Couldn't mark 'this task' done" banner in the user's menu.
    monkeypatch.setenv("JT_PANEL_CACHE", str(tmp_path / "panel-cache.json"))
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


def test_review_count_without_items_points_to_terminal(plugin):
    """An older jt without review items still shows the count, with a way in,
    and never shows raw internal reasons."""
    s = snap(badge={"due": 0, "reviews": 2},
             review={"pending": 2, "oldest_days": 1,
                     "top": {"review_id": 4, "reason": "needs your confirmation: rejected for #3"}})
    lines = plugin.render(s, None, NOW)
    assert any(x.startswith("Needs your decision (2)") for x in lines)
    assert any(x.startswith("+2 more in Terminal") for x in lines)
    assert not any("needs your confirmation" in x for x in lines)


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
    now = datetime.now(timezone.utc)
    assert any("Retry" in line for line in plugin.render(plugin.read_cache(), None, now))
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



# ---------------------------------------------------------------- review UX

def test_plugin_tests_never_use_the_real_cache(plugin, tmp_path):
    assert str(plugin.CACHE).startswith(str(tmp_path))


def test_old_failure_banner_expires(plugin):
    old = (NOW - timedelta(minutes=30)).isoformat()
    fresh = (NOW - timedelta(minutes=2)).isoformat()
    stale = plugin.render(snap(failure={"message": "Couldn't mark 'X' done. Try again",
                                        "args": ["done", "1"], "ts": old}), None, NOW)
    assert not any("Couldn't" in x for x in stale)
    recent = plugin.render(snap(failure={"message": "Couldn't mark 'X' done. Try again",
                                         "args": ["done", "1"], "ts": fresh}), None, NOW)
    assert any("Couldn't" in x for x in recent)


def item(rid=7, choices=None, url="https://mail.google.com/mail/u/0/#all/abc"):
    return {"review_id": rid, "received_on": "2026-09-05", "from": "hireflix.com",
            "subject": "Thank you for completing your video interview",
            "summary": "Looks like an interview or screen invite from Ramp, but jt isn't sure.",
            "gmail_url": url,
            "choices": choices if choices is not None else
                       [{"label": "Record interview for Ramp · CX Agent", "app_id": 10}]}


def test_review_items_render_in_plain_english_with_one_click_choices(plugin):
    s = snap(badge={"due": 0, "reviews": 1},
             review={"pending": 1, "oldest_days": 0, "top": None, "items": [item()]})
    lines = plugin.render(s, None, NOW)
    text = "\n".join(lines)
    assert "Needs your decision" in text
    assert "Thank you for completing your video interview" in text
    assert "jt isn't sure" in text
    assert "classifier returned" not in text and "null" not in text
    approve = next(x for x in lines if "Record interview for Ramp" in x)
    assert 'param1=approve param2="7" param3="10"' in approve
    dismiss = next(x for x in lines if x.startswith("--Dismiss"))
    assert 'param1=dismiss-review param2="7"' in dismiss
    assert any(x.startswith("--Open email") for x in lines)


def test_review_item_with_no_safe_choice_offers_dismiss_and_email_only(plugin):
    s = snap(badge={"due": 0, "reviews": 1},
             review={"pending": 1, "oldest_days": 0, "top": None, "items": [item(choices=[])]})
    lines = plugin.render(s, None, NOW)
    assert not any("param1=approve" in x for x in lines)
    assert any(x.startswith("--Dismiss") for x in lines)


def test_rows_with_submenus_are_not_greyed_out(plugin):
    """SwiftBar greys out rows with no action; give parents a harmless one."""
    s = snap(badge={"due": 0, "reviews": 1},
             review={"pending": 1, "oldest_days": 0, "top": None, "items": [item()]},
             suggestions=[{"application_id": 9, "company": "Ramp", "role": "CX Agent",
                           "kind": "FOLLOW UP", "reason": "11 business days quiet"}])
    lines = plugin.render(s, None, NOW)
    parents = [x for x in lines if x.startswith(("Thank you for completing", "Follow Up:"))]
    assert parents and all("refresh=true" in x for x in parents)


def test_approve_and_dismiss_clicks_call_jt_review(plugin, monkeypatch):
    calls = []
    class R:
        returncode, stdout, stderr = 0, json.dumps({"ok": True}), ""
    monkeypatch.setattr(plugin, "jt", lambda *a, **k: calls.append(a) or R())
    assert plugin.handle_click(["approve", "7", "10"]) == 0
    assert plugin.handle_click(["dismiss-review", "7"]) == 0
    assert calls[0] == ("review", "approve", "7", "--app-id", "10", "--json")
    assert calls[1] == ("review", "dismiss", "7", "--note", "dismissed from the menu bar", "--json")


def test_failed_approve_shows_the_reason_and_expires(plugin, monkeypatch):
    class R:
        returncode, stdout, stderr = 1, json.dumps({"ok": False, "error": "illegal transition tech -> screen"}), ""
    monkeypatch.setattr(plugin, "jt", lambda *a, **k: R())
    plugin.save_cache(snap())
    assert plugin.handle_click(["approve", "7", "10"]) == 1
    cached = plugin.read_cache()
    assert "illegal transition" in cached["failure"]["message"]
    assert cached["failure"]["ts"]


def test_new_application_choice_asks_for_the_lane(plugin, monkeypatch):
    it = item(rid=21, choices=[{"label": "Add TD Bank · Associate Banker II as a new application",
                                "new_app": True, "lanes": ["swe", "ops", "comms"]}])
    s = snap(badge={"due": 0, "reviews": 1},
             review={"pending": 1, "oldest_days": 0, "top": None, "items": [it]})
    lines = plugin.render(s, None, NOW)
    assert any(x.startswith("--Add TD Bank · Associate Banker II as a new application") for x in lines)
    ops = next(x for x in lines if x.startswith("----Ops"))
    assert 'param1=add-app param2="21" param3="ops"' in ops
    calls = []
    class R:
        returncode, stdout, stderr = 0, json.dumps({"ok": True}), ""
    monkeypatch.setattr(plugin, "jt", lambda *a, **k: calls.append(a) or R())
    assert plugin.handle_click(["add-app", "21", "ops"]) == 0
    assert calls[0] == ("review", "add-app", "21", "--lane", "ops", "--json")


def test_close_out_suggestion_offers_close_choices_and_keep_open(plugin, monkeypatch):
    s = snap(suggestions=[{"application_id": 9, "company": "Ramp", "role": "CX Agent",
                           "kind": "CLOSE OUT", "reason": "55 business days with no reply",
                           "close_choices": [{"label": "Mark as no response", "kind": "ghosted"},
                                             {"label": "I withdrew", "kind": "withdrawn"}]}])
    lines = plugin.render(s, None, NOW)
    assert any(x.startswith("Close out?: Ramp") for x in lines)
    close = next(x for x in lines if x.startswith("--Mark as no response"))
    assert 'param1=close param2="9" param3="ghosted"' in close
    assert any(x.startswith("--Keep it open") for x in lines)
    assert not any(x.startswith("--Remind me tomorrow") for x in lines)
    calls = []
    class R:
        returncode, stdout, stderr = 0, json.dumps({"ok": True}), ""
    monkeypatch.setattr(plugin, "jt", lambda *a, **k: calls.append(a) or R())
    assert plugin.handle_click(["close", "9", "ghosted"]) == 0
    assert calls[0] == ("log", "9", "ghosted", "--note", "closed from the menu bar", "--json")


def test_more_suggestions_than_shown_are_counted(plugin):
    sug = [{"application_id": i, "company": f"Co{i}", "role": "R", "kind": "CLOSE OUT",
            "reason": "50 business days with no reply", "close_choices": []} for i in range(10)]
    lines = plugin.render(snap(suggestions=sug), None, NOW)
    assert sum(1 for x in lines if x.startswith("Close out?")) == 3
    assert any(x.startswith("+7 more to close out") for x in lines)


def test_record_button_sends_the_expected_stage(plugin, monkeypatch):
    it = item(rid=7, choices=[{"label": "Record interview for Ramp · CX Agent", "app_id": 10,
                               "stage": "screen"}])
    s = snap(badge={"due": 0, "reviews": 1},
             review={"pending": 1, "oldest_days": 0, "top": None, "items": [it]})
    rec = next(x for x in plugin.render(s, None, NOW) if "Record interview" in x)
    assert 'param1=approve param2="7" param3="10" param4="screen"' in rec
    calls = []
    class R:
        returncode, stdout, stderr = 0, json.dumps({"ok": True}), ""
    monkeypatch.setattr(plugin, "jt", lambda *a, **k: calls.append(a) or R())
    assert plugin.handle_click(["approve", "7", "10", "screen"]) == 0
    assert calls[0] == ("review", "approve", "7", "--app-id", "10", "--expected-stage", "screen", "--json")



def test_new_applications_section_with_add_and_not_mine(plugin, monkeypatch):
    s = snap(badge={"due": 0, "reviews": 0, "candidates": 1},
             candidates=[{"candidate_id": 5, "company": "Figma", "role": "Product Designer",
                          "applied_on": "2026-11-14", "can_add": True}])
    lines = plugin.render(s, None, NOW)
    assert lines[0] == "jt · 1?"
    text = "\n".join(lines)
    assert "New applications found (1)" in text and "Figma · Product Designer" in text
    comms = next(x for x in lines if x.startswith("----Comms"))
    assert 'param1=accept-candidate param2="5" param3="comms"' in comms
    assert any(x.startswith("--Not mine") and 'param1=reject-candidate param2="5"' in x for x in lines)
    calls = []
    class R:
        returncode, stdout, stderr = 0, json.dumps({"ok": True}), ""
    monkeypatch.setattr(plugin, "jt", lambda *a, **k: calls.append(a) or R())
    assert plugin.handle_click(["accept-candidate", "5", "comms"]) == 0
    assert plugin.handle_click(["reject-candidate", "5"]) == 0
    assert calls[0] == ("candidates", "accept", "5", "--lane", "comms", "--json")
    assert calls[1] == ("candidates", "reject", "5", "--note", "not mine (menu bar)", "--json")


def test_candidate_without_role_offers_only_not_mine_and_terminal(plugin):
    s = snap(badge={"due": 0, "reviews": 0, "candidates": 1},
             candidates=[{"candidate_id": 6, "company": "Acme", "role": None,
                          "applied_on": "2026-11-14", "can_add": False}])
    lines = plugin.render(s, None, NOW)
    assert not any("accept-candidate" in x for x in lines)
    assert any(x.startswith("--Not mine") for x in lines)


def test_click_with_missing_arguments_fails_cleanly(plugin):
    assert plugin.handle_click(["accept-candidate"]) == 2
    assert plugin.handle_click(["approve", "7"]) == 2
