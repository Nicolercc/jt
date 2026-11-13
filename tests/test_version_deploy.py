"""PRD Now-E3: you can always tell which code production runs, and deploying
is one command that backs up first and refuses unsafe states."""

import json
import os
import subprocess
from pathlib import Path

import pytest

import jt

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts/deploy.sh"


def test_version_reports_commit_and_schema(capsys):
    jt.main_args(["version", "--json"])
    v = json.loads(capsys.readouterr().out)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert v["commit"] == head
    assert v["schema"] == jt.SCHEMA_VERSION
    assert isinstance(v["dirty"], bool)


def test_version_outside_git_says_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(jt, "REPO_DIR", tmp_path)
    assert jt.version_info()["commit"] == "unknown"


def test_today_json_carries_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JT_DB", str(tmp_path / "jobs.db"))
    jt.main_args(["today", "--json"])
    s = json.loads(capsys.readouterr().out)
    assert s["version"]["schema"] == jt.SCHEMA_VERSION and s["version"]["commit"]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def fake_prod(tmp_path):
    """A clone standing in for ~/Developer/jt, with origin one commit ahead."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "README").write_text("v1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "v1")
    prod = tmp_path / "prod"
    _git(tmp_path, "clone", "-q", str(origin), str(prod))
    (origin / "README").write_text("v2\n")
    _git(origin, "commit", "-q", "-am", "v2")
    return prod


def deploy(prod, *args, home=None):
    env = {**os.environ, "JT_DEPLOY_REPO": str(prod), "JT_PYTHON": "/usr/bin/false",
           "JT_BACKUP_DIR": str(prod.parent / "backups"), "JT_LAUNCHCTL": "/usr/bin/true"}
    return subprocess.run(["bash", str(DEPLOY), *args], capture_output=True, text=True, env=env)


def test_deploy_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(DEPLOY)]).returncode == 0


def test_deploy_refuses_a_dirty_production_checkout(fake_prod):
    (fake_prod / "README").write_text("local edit\n")
    r = deploy(fake_prod)
    assert r.returncode != 0 and "uncommitted" in r.stderr
    assert not (fake_prod.parent / "backups").exists()      # stopped before touching anything


def test_dry_run_shows_the_plan_and_changes_nothing(fake_prod):
    before = subprocess.run(["git", "-C", str(fake_prod), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout
    r = deploy(fake_prod, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "v2" in r.stdout and "would" in r.stdout.lower()
    after = subprocess.run(["git", "-C", str(fake_prod), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout
    assert before == after
    assert not (fake_prod.parent / "backups").exists()


def test_deploy_stops_if_the_backup_fails(fake_prod):
    """JT_PYTHON=/usr/bin/false makes `jt backup` fail: nothing may be pulled."""
    r = deploy(fake_prod)
    assert r.returncode != 0 and "backup" in r.stderr.lower()
    log = subprocess.run(["git", "-C", str(fake_prod), "log", "--oneline", "-1"],
                         capture_output=True, text=True).stdout
    assert "v1" in log
