#!/usr/bin/env python3
"""
Run the redacted classifier eval set.

By default this calls the configured classifier model, then runs the exact same
validation path as classify.py. `--use-expected` is for testing the evaluator
itself without live API credentials.
"""

import argparse
import importlib.resources
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import classify
import jt
import transitions
from evals import dataset

Z95 = 1.96


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score 95% interval for k successes in n trials."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def summarize(fixtures: list[dict], actual_by_id: dict[str, dict],
              review_reasons: dict[str, str]) -> dict:
    """Score persisted outcomes against labels.

    A commit is correct only if kind, company and role all match. Anything else
    that reached `events` is a wrong commit, and a wrong commit of a terminal
    kind is the error DESIGN.md says must be zero.
    """
    expected = {fx["id"]: (fx.get("expect") or {}) for fx in fixtures}
    should = {i for i, e in expected.items() if e.get("should_commit")}

    def correct(i: str) -> bool:
        a, e = actual_by_id.get(i), expected.get(i, {})
        if a is None or i not in should:
            return False
        return (a["kind"] == e.get("kind") and a["company"] == e.get("company")
                and a["role"] == e.get("role", "Backend Engineer"))

    true_commits = {i for i in actual_by_id if correct(i)}
    wrong = set(actual_by_id) - true_commits
    per_kind: dict[str, dict[str, int]] = {}
    for i, e in expected.items():
        k = per_kind.setdefault(e.get("kind") or dataset.NONE_KIND,
                                {"n": 0, "correct": 0, "wrong": 0, "reviewed": 0, "missed": 0})
        k["n"] += 1
        k["correct"] += i in true_commits
        k["wrong"] += i in wrong
        k["reviewed"] += i in review_reasons
        k["missed"] += i in should and i not in true_commits

    n = len(fixtures)
    return {
        "n": n,
        "precision": len(true_commits) / len(actual_by_id) if actual_by_id else 1.0,
        "recall": len(true_commits) / len(should) if should else 1.0,
        "precision_ci": wilson(len(true_commits), len(actual_by_id)),
        "recall_ci": wilson(len(true_commits), len(should)),
        "wrong_commits": len(wrong),
        "wrong_commits_per_100": 100 * len(wrong) / n if n else 0.0,
        "wrong_commits_ci": wilson(len(wrong), n),
        "wrong_terminal_commits": sum(
            1 for i in wrong if actual_by_id[i]["kind"] in transitions.TERMINAL_STATES),
        "review_rate": len(review_reasons) / n if n else 0.0,
        "per_kind": dict(sorted(per_kind.items())),
    }


def recognition_metrics(fixtures, actual, reviewed, held):
    def rate(count, n):
        return {"count": count, "n": n, "value": count / n if n else None,
                "ci": wilson(count, n)}

    def score(group):
        counts = dict.fromkeys(
            ("correct_auto", "correct_held", "wrong_auto", "wrong_terminal_auto", "true_miss"), 0)
        workload = recognize = 0
        for fixture in group:
            key = fixture["id"]
            expected = fixture.get("expect") or {}
            should = expected.get("should_recognize", expected.get("should_commit", False))
            recognize += bool(should)
            workload += key in reviewed
            outcome = actual.get(key) or held.get(key)
            correct = bool(should and outcome and
                           (outcome["kind"], outcome["company"], outcome["role"]) ==
                           (expected.get("kind"), expected.get("company"),
                            expected.get("role", "Backend Engineer")))
            if key in actual:
                counts["correct_auto" if correct else "wrong_auto"] += 1
                counts["wrong_terminal_auto"] += not correct and outcome["kind"] in transitions.TERMINAL_STATES
            elif correct:
                counts["correct_held"] += 1
            elif should:
                counts["true_miss"] += 1
        coverage = rate(counts["correct_auto"] + counts["correct_held"], recognize)
        return {**counts, "should_recognize": recognize, "coverage": coverage["value"],
                "coverage_rate": coverage, "review_workload": rate(workload, len(group)),
                "wrong_auto_rate": rate(counts["wrong_auto"], len(group)),
                "n": len(group)}

    groups: dict[str, list[dict]] = {}
    for fixture in fixtures:
        kind = (fixture.get("expect") or {}).get("kind") or dataset.NONE_KIND
        groups.setdefault(kind, []).append(fixture)
    return {**score(fixtures), "per_kind": {k: score(v) for k, v in sorted(groups.items())}}


def _fixture_lines(path: Path):
    if path.exists():
        return path.open(encoding="utf-8")
    if path.name == "fixtures.example.jsonl":
        return importlib.resources.files("evals").joinpath(path.name).open(
            encoding="utf-8")
    raise FileNotFoundError(path)


def load_fixtures(path: Path) -> list[dict]:
    fixtures = []
    with _fixture_lines(path) as f:
        for line in f:
            line = line.strip()
            if line:
                fixtures.append(json.loads(line))
    return fixtures


def _insert_app(conn, company: str, role: str = "Backend Engineer") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO applications (company, role, lane, applied_on) VALUES (?,?,?,?)",
        (company, role, "swe", "2026-08-01"),
    )


def seed_db(conn, fixtures: list[dict]) -> None:
    for fx in fixtures:
        expect = fx.get("expect") or {}
        company = expect.get("company")
        if not company:
            continue
        roles = fx.get("application_roles", [expect.get("role", "Backend Engineer")] if expect.get("should_commit") else [])
        for role in roles:
            _insert_app(conn, company, role)
        prior = fx.get("prior_event")
        if prior:
            conn.execute(
                """INSERT INTO events (application_id, occurred_on, kind)
                   SELECT id, '2026-08-01', ? FROM applications WHERE company = ? AND role = ?""",
                (prior, company, roles[0]),
            )
    for fx in fixtures:
        conn.execute(
            """INSERT INTO raw_messages
               (gmail_msg_id, received_on, sender, subject, body, processed)
               VALUES (?,?,?,?,?,0)""",
            (fx["id"], "2026-08-02", fx.get("sender"), fx.get("subject"), fx.get("body", "")),
        )
    conn.commit()


def expected_response(fixtures: list[dict]) -> str:
    proposals = []
    for fx in fixtures:
        expect = fx.get("expect") or {}
        kind = fx.get("proposal_kind", expect.get("kind"))
        if not kind:
            continue
        body = fx.get("body", "")
        evidence = " ".join(body.split()[: min(8, len(body.split()))])
        proposals.append({
            "gmail_msg_id": fx["id"],
            "company": expect.get("company") or "",
            "role_hint": fx.get("role_hint", expect.get("role", "Backend Engineer")),
            "kind": kind,
            "occurred_on": "2026-08-02",
            "confidence": 0.99,
            "evidence": evidence,
        })
    return json.dumps(proposals)


POLICY_ENV = "CLASSIFIER_AUTO_COMMIT_TERMINAL"


def run_eval(fixtures: list[dict], *, use_expected: bool = False, client=None,
             auto_commit_consequential: bool | None = None,
             raw_response: str | None = None) -> dict:
    """Score one classifier run against the labels.

    auto_commit_consequential: None uses the environment (the product
    default); True/False pins the DESIGN D3 policy for this run only.
    raw_response: reuse an earlier run's model output, so two policies can be
    compared on identical proposals with one model call.
    """
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("JT_DB")
        old_policy = os.environ.get(POLICY_ENV)
        os.environ["JT_DB"] = str(Path(d) / "eval.db")
        if auto_commit_consequential is not None:
            os.environ[POLICY_ENV] = "1" if auto_commit_consequential else "0"
        try:
            policy_auto = classify.auto_commit_consequential()
            conn = jt.connect()
            seed_db(conn, fixtures)
            messages = classify.load_unprocessed(conn, len(fixtures))
            if raw_response is not None:
                raw = raw_response
            elif use_expected:
                raw = expected_response(fixtures)
            else:
                raw = classify.call_model(client or classify.build_classifier_client(), messages)
            verdicts = classify.verdicts_for(conn, messages, raw)
            classify.apply_verdicts(conn, verdicts)
            actual_rows = conn.execute("""SELECT e.gmail_msg_id, e.kind, a.company, a.role
                FROM events e JOIN applications a ON a.id = e.application_id
                WHERE e.gmail_msg_id IS NOT NULL""").fetchall()
            review_reasons = dict(conn.execute("SELECT gmail_msg_id, reason FROM review_queue"))
            applications = {row["id"]: row for row in conn.execute("SELECT id, company, role FROM applications")}
            held = {
                v.gmail_msg_id: {"kind": v.proposal["kind"], "company": applications[v.application_id]["company"],
                                 "role": applications[v.application_id]["role"]}
                for v in verdicts
                if v.proposal and v.application_id in applications
                and review_reasons.get(v.gmail_msg_id, "").startswith(classify.CONFIRMATION_REASON)
            }
            candidates = conn.execute("SELECT COUNT(*) FROM application_candidates").fetchone()[0]
        finally:
            if "conn" in locals():
                conn.close()
            if old_policy is None:
                os.environ.pop(POLICY_ENV, None)
            else:
                os.environ[POLICY_ENV] = old_policy
            if old is None:
                os.environ.pop("JT_DB", None)
            else:
                os.environ["JT_DB"] = old

    expected = {fx["id"]: (fx.get("expect") or {}) for fx in fixtures}
    actual_by_id = {row["gmail_msg_id"]: dict(row) for row in actual_rows}
    expected_commits = {
        msg_id: exp for msg_id, exp in expected.items() if exp.get("should_commit")
    }
    true_commits = {
        msg_id
        for msg_id, actual in actual_by_id.items()
        if msg_id in expected_commits
        and actual["kind"] == expected_commits[msg_id].get("kind")
        and actual["company"] == expected_commits[msg_id].get("company")
        and actual["role"] == expected_commits[msg_id].get("role", "Backend Engineer")
    }
    false_commits = set(actual_by_id) - true_commits
    missed_commits = set(expected_commits) - true_commits

    precision = len(true_commits) / len(actual_by_id) if actual_by_id else 1.0
    recall = len(true_commits) / len(expected_commits) if expected_commits else 1.0
    summary = summarize(fixtures, actual_by_id, review_reasons)
    recognition = recognition_metrics(fixtures, actual_by_id, review_reasons, held)
    for kind, metrics in recognition["per_kind"].items():
        summary["per_kind"][kind].update(metrics)
    recognition["per_kind"] = summary["per_kind"]
    return {
        **summary,
        **recognition,
        "fixtures": len(fixtures),
        "expected_commits": len(expected_commits),
        "actual_commits": len(actual_by_id),
        "correct_commits": len(true_commits),
        "application_candidates": candidates,
        "precision": precision,
        "recall": recall,
        "misclassifications_reached_events": len(false_commits),
        "missed_expected_commits": len(missed_commits),
        "reviewed": len(review_reasons),
        "discrepancies": [
            (msg_id, expected.get(msg_id, {}).get("kind"), actual_by_id.get(msg_id, {}).get("kind"),
             review_reasons.get(msg_id, "unexpected persisted outcome"))
            for msg_id in sorted(false_commits | missed_commits)
        ],
        "review_reasons": review_reasons,
        "raw_response": raw,
        "policy": "auto-commit consequential kinds" if policy_auto
                  else "review consequential kinds",
        "held_for_confirmation": sum(
            1 for r in review_reasons.values() if r.startswith(classify.CONFIRMATION_REASON)),
    }


def _pct_ci(value: float, ci: tuple[float, float]) -> str:
    return f"{value:.1%}  (95% CI {ci[0]:.1%}–{ci[1]:.1%})"


def print_report(report: dict, label: str = "all") -> None:
    print(f"set: {label}")
    print(f"fixtures: {report['fixtures']}")
    print(f"policy: {report['policy']}")
    for kind, row in [("total", report), *report["per_kind"].items()]:
        print(f"{kind} (n={row['n']}): " + ", ".join(
            f"{key}={row[key]}" for key in
            ("correct_auto", "correct_held", "wrong_auto", "wrong_terminal_auto", "true_miss")))
        for key, label in (("wrong_auto_rate", "Wrong auto per 100 (target ≤ 2)"),
                           ("review_workload", "Reviews per 100"),
                           ("coverage_rate", "Coverage")):
            metric = row[key]
            value = metric["value"]
            lo, hi = metric["ci"]
            shown = "n/a" if value is None else f"{100 * value:.1f}"
            print(f"  {label}: {shown} (95% CI {100 * lo:.1f}–{100 * hi:.1f}, n={metric['n']})")
    print("Wrong terminal auto target: 0")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="run jt classifier evals")
    p.add_argument("fixtures", nargs="?", default="evals/fixtures.jsonl")
    p.add_argument("--use-expected", action="store_true",
                   help="test the eval harness without calling the model")
    p.add_argument("--set", dest="which", choices=("all", "dev", "holdout"), default="all",
                   help="score only one side of the frozen split (needs --split file)")
    p.add_argument("--split", type=Path, default=dataset.DEFAULT_SPLIT)
    p.add_argument("--policy", choices=("default", "review", "auto", "compare"), default="default",
                   help="DESIGN D3: review or auto-commit consequential kinds; compare runs "
                        "both on one model response")
    return p


def main() -> None:
    args = build_parser().parse_args()
    path = Path(args.fixtures)
    if not path.exists() and path.name != "fixtures.example.jsonl":
        raise SystemExit(
            f"error: {path} does not exist. Copy/redact fixtures from "
            "evals/fixtures.example.jsonl first."
        )
    fixtures = load_fixtures(path)
    if args.which != "all":
        try:
            fixtures = dataset.select_set(fixtures, dataset.load_split(args.split), args.which)
        except ValueError as e:
            raise SystemExit(f"error: {e}")
    if args.which == "holdout":
        print("HOLDOUT: report these numbers. Do not change prompts or thresholds "
              "because of them.\n")
    if args.policy == "compare":
        before = run_eval(fixtures, use_expected=args.use_expected, auto_commit_consequential=True)
        after = run_eval(fixtures, use_expected=args.use_expected,
                         auto_commit_consequential=False, raw_response=before["raw_response"])
        print("=== BEFORE D3: consequential kinds auto-commit ===")
        print_report(before, args.which)
        print("\n=== AFTER D3: consequential kinds go to review (same model output) ===")
        print_report(after, args.which)
        print(f"\nD3 effect: wrong terminal commits {before['wrong_terminal_commits']} -> "
              f"{after['wrong_terminal_commits']}; extra reviews "
              f"{after['held_for_confirmation']} of {after['n']} emails")
        return
    pinned = {"auto": True, "review": False}.get(args.policy)
    print_report(run_eval(fixtures, use_expected=args.use_expected,
                          auto_commit_consequential=pinned), args.which)


if __name__ == "__main__":
    main()
