import pytest

import classify
from evals.run import load_fixtures, run_eval


def test_eval_harness_runs_against_redacted_example():
    fixtures = load_fixtures(__import__("pathlib").Path("evals/fixtures.example.jsonl"))
    # Harness self-test: pin the pre-D3 policy so every expected commit can land.
    report = run_eval(fixtures, use_expected=True, auto_commit_consequential=True)

    assert report["fixtures"] == 8
    assert report["misclassifications_reached_events"] == 0
    assert report["precision"] == 1.0
    assert report["recall"] == 1.0
    assert "ambiguous role" in report["review_reasons"]["fx05"]


def test_eval_counts_wrong_kind_as_misclassification():
    class ToolUseBlock:
        type = "tool_use"
        name = classify.TOOL_NAME
        input = {"proposals": [{
            "gmail_msg_id": "fx",
            "company": "Company A",
            "role_hint": "Backend Engineer",
            "kind": "screen",
            "occurred_on": "2026-08-02",
            "confidence": 0.99,
            "evidence": "we received your application",
        }]}

    class FakeClient:
        class Messages:
            def create(self, **kwargs):
                class Response:
                    pass

                r = Response()
                r.content = [ToolUseBlock()]
                return r

        @property
        def messages(self):
            return self.Messages()

    fixtures = [{
        "id": "fx",
        "sender": "jobs@example.com",
        "subject": "Thanks",
        "body": "Hi, we received your application today.",
        "expect": {"kind": "confirmed", "company": "Company A", "should_commit": True},
    }]

    report = run_eval(fixtures, client=FakeClient())

    assert report["actual_commits"] == 1
    assert report["precision"] == 0.0
    assert report["misclassifications_reached_events"] == 1


# ---------------------------------------------------------------- M1 metrics

from evals.run import summarize, wilson


def test_wilson_interval_matches_known_values():
    lo, hi = wilson(14, 15)
    assert round(lo, 3) == 0.702 and round(hi, 3) == 0.988
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(0, 20)
    assert lo == 0.0 and round(hi, 3) == 0.161


def _fx(i, kind, company="Acme", role="Engineer"):
    return {"id": f"f{i}", "expect": {
        "kind": kind, "company": company if kind else None,
        "role": role if kind else None, "should_commit": kind is not None}}


def test_summarize_counts_per_kind_and_terminal_errors():
    fixtures = [
        _fx(1, "confirmed"), _fx(2, "confirmed"), _fx(3, "rejected"),
        _fx(4, None), _fx(5, "screen"),
    ]
    actual = {
        "f1": {"kind": "confirmed", "company": "Acme", "role": "Engineer"},  # right
        "f2": {"kind": "rejected", "company": "Acme", "role": "Engineer"},   # wrong, terminal
        "f4": {"kind": "confirmed", "company": "Acme", "role": "Engineer"},  # wrong, non-status mail
    }
    reviewed = {"f3": "low confidence", "f5": "ambiguous role"}

    s = summarize(fixtures, actual, reviewed)

    assert s["n"] == 5
    assert s["wrong_commits"] == 2
    assert s["wrong_commits_per_100"] == 40.0
    assert s["wrong_terminal_commits"] == 1
    assert s["review_rate"] == 0.4
    assert s["precision"] == pytest.approx(1 / 3)
    assert s["recall"] == pytest.approx(1 / 4)
    conf = s["per_kind"]["confirmed"]
    assert conf == {"n": 2, "correct": 1, "wrong": 1, "reviewed": 0, "missed": 1}
    assert s["per_kind"]["none"] == {"n": 1, "correct": 0, "wrong": 1, "reviewed": 0, "missed": 0}
    assert s["per_kind"]["rejected"]["reviewed"] == 1
    assert s["precision_ci"][0] < s["precision"] < s["precision_ci"][1]


def test_run_eval_report_includes_m1_metrics():
    fixtures = load_fixtures(__import__("pathlib").Path("evals/fixtures.example.jsonl"))
    report = run_eval(fixtures, use_expected=True)
    for key in ("per_kind", "wrong_commits_per_100", "wrong_terminal_commits",
                "review_rate", "precision_ci", "recall_ci"):
        assert key in report
    assert report["wrong_terminal_commits"] == 0



# ---------------------------------------------------------------- D3 compare

def test_eval_compares_both_policies_from_one_model_response():
    """Same model output, two policies: the before/after for D3 without
    paying for (or varying) a second model call."""
    fixtures = load_fixtures(__import__("pathlib").Path("evals/fixtures.example.jsonl"))
    auto = run_eval(fixtures, use_expected=True, auto_commit_consequential=True)
    review = run_eval(fixtures, use_expected=True, auto_commit_consequential=False,
                      raw_response=auto["raw_response"])
    assert auto["raw_response"] == review["raw_response"]
    assert review["held_for_confirmation"] >= 1
    assert review["actual_commits"] == auto["actual_commits"] - review["held_for_confirmation"]
    assert review["wrong_terminal_commits"] == 0


def test_eval_policy_flag_does_not_leak_into_the_environment(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_AUTO_COMMIT_TERMINAL", raising=False)
    fixtures = load_fixtures(__import__("pathlib").Path("evals/fixtures.example.jsonl"))
    run_eval(fixtures, use_expected=True, auto_commit_consequential=True)
    import os
    assert "CLASSIFIER_AUTO_COMMIT_TERMINAL" not in os.environ



def test_report_names_the_policy_that_actually_ran(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_AUTO_COMMIT_TERMINAL", "1")
    fixtures = load_fixtures(__import__("pathlib").Path("evals/fixtures.example.jsonl"))
    assert run_eval(fixtures, use_expected=True,
                    auto_commit_consequential=False)["policy"] == "review consequential kinds"
    assert run_eval(fixtures, use_expected=True)["policy"] == "auto-commit consequential kinds"


def test_d3_offer_counts_as_held_recognition():
    fixtures = [{
        "id": "offer-1",
        "body": "We are pleased to offer you the Engineer role at Example.",
        "expect": {"kind": "offer", "company": "Example", "role": "Engineer",
                   "should_commit": True},
        "prior_event": "onsite",
    }]
    held = run_eval(fixtures, use_expected=True, auto_commit_consequential=False)
    auto = run_eval(fixtures, raw_response=held["raw_response"], auto_commit_consequential=True)
    assert held["correct_held"] == held["per_kind"]["offer"]["correct_held"] == 1
    assert held["correct_auto"] == held["true_miss"] == 0
    assert held["coverage"] == auto["coverage"] == 1
    assert auto["correct_auto"] == 1
    assert auto["correct_held"] == 0
