"""Unit tests for the ``code-agent`` worker loop (app/dockwatch/services/code_worker.py).

Runs a real local git repo for checkout while stubbing the trove control-plane
client; the python stage command that fails (``pip install -e .`` with no
project file) makes the pipeline deterministically end in ``error``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.dockwatch.services import code_worker as cw


def make_git_repo(path: Path, commit_msg: str = "initial") -> str:
    """Create a git repo with one commit on a ``main`` branch; return file URL."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("# project\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", commit_msg], check=True)
    subprocess.run(["git", "-C", str(path), "branch", "-M", "main"], check=True)
    return path.resolve().as_uri()


class FakeTrove:
    """In-memory stand-in for the real TroveClient (no HTTP)."""

    def __init__(self, cfg: cw.WorkerConfig, repo_url: str, agent_id: int = 7) -> None:
        self._cfg = cfg
        self._repo_url = repo_url
        self.agent_id = agent_id
        self._latest_run: dict | None = None
        self._projects: list[dict] = [
            {
                "id": 1,
                "agent_id": agent_id,
                "status": "queued",
                "repo_url": repo_url,
                "branch": "main",
            }
        ]
        self.ingested: list[dict] = []
        self.heartbeats: list[tuple[str, str | None]] = []
        self.closed = False

    def resolve_agent(self) -> dict:
        meta = json.dumps({"language": "python", "model": "qwen2.5-coder:1.5b"})
        return {"id": self.agent_id, "name": self._cfg.agent_name, "metadata_json": meta}

    def heartbeat(self, status: str, snippet: str | None = None) -> None:
        self.heartbeats.append((status, snippet))

    def assigned_projects(self) -> list[dict]:
        return [dict(p) for p in self._projects]

    def latest_run_for(self, project_id: int) -> dict | None:
        return self._latest_run

    def ingest(self, payload: dict) -> dict:
        self.ingested.append(payload)
        return {"id": len(self.ingested)}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def cfg(tmp_path) -> cw.WorkerConfig:
    work = tmp_path / "work"
    work.mkdir()
    return cw.WorkerConfig(
        trove_url="http://trove:8000",
        agent_name="ray",
        workspace=str(work),
        interval=60.0,
        fix_depth=0,
        timeout=90.0,
        once=True,
    )


def test_run_once_plain_pipeline(cfg, tmp_path, monkeypatch) -> None:
    repo_url = make_git_repo(tmp_path / "proj")
    fake = FakeTrove(cfg, repo_url)
    monkeypatch.setattr(cw, "TroveClient", lambda c: fake)

    summary = cw.run_once(cfg)

    assert "error" in summary
    assert fake.closed
    # running then final ingest
    assert [r["status"] for r in fake.ingested] == ["running", "error"]
    running, final = fake.ingested
    assert running["stages"] == []
    assert running["repo_url"] == repo_url
    assert final["commit"]
    stages = final["stages"]
    assert stages[0]["stage"] == "checkout" and stages[0]["status"] == "ok"
    # deps (pip install -e . with no project file) fails -> the rest skip
    assert stages[1]["stage"] == "deps" and stages[1]["status"] == "error"
    assert all(s["status"] == "skipped" for s in stages[2:])
    assert [s["stage"] for s in stages] == list(cw.STAGES)
    # heartbeat lifecycle
    statuses = [h[0] for h in fake.heartbeats]
    assert "working" in statuses and "idle" in statuses


def test_run_once_idle_without_project(cfg, tmp_path, monkeypatch) -> None:
    repo_url = make_git_repo(tmp_path / "proj")
    fake = FakeTrove(cfg, repo_url)
    fake._projects = []
    monkeypatch.setattr(cw, "TroveClient", lambda c: fake)

    assert cw.run_once(cfg) == "idle"
    assert fake.ingested == []
    assert fake.heartbeats == []


def test_run_once_skips_unchanged_commit(cfg, tmp_path, monkeypatch) -> None:
    repo_url = make_git_repo(tmp_path / "proj", commit_msg="old")
    commit = (
        subprocess.check_output(["git", "-C", str(tmp_path / "proj"), "rev-parse", "HEAD"])
        .decode()
        .strip()
    )
    fake = FakeTrove(cfg, repo_url)
    fake._latest_run = {"status": "ok", "commit": commit}
    monkeypatch.setattr(cw, "TroveClient", lambda c: fake)

    assert cw.run_once(cfg) == "unchanged"
    assert fake.ingested == []


def test_build_pipeline_stages_runs_fix_loop_once(cfg, monkeypatch) -> None:
    """The agentic fix loop re-runs a failing stage when --fix-depth > 0."""
    repo_url = make_git_repo(Path(cfg.workspace).parent / "proj2")
    runner = cw.RepoRunner(cfg, repo_url, "main", "proj2")
    runner.language = "python"
    cfg.fix_depth = 2

    calls = {"attempts": 0}

    def fake_attempt_fix(stage, detail) -> str:
        calls["attempts"] += 1
        return "ok" if calls["attempts"] == 1 else "error"

    monkeypatch.setattr(runner, "attempt_fix", fake_attempt_fix)

    # Force a failure at the 'test' stage (a fixable stage); /bin/false exits nonzero.
    monkeypatch.setitem(
        cw.LANGUAGE_STAGES,
        "python",
        {"test": ["false"]},
    )
    # checkout records ok; test fails once then the fix loop "fixes" it.
    stages = cw.build_pipeline_stages(runner, cfg)
    test_stage = next(s for s in stages if s["stage"] == "test")
    # first attempt returns ok -> loop stops after 1 attempt
    assert calls["attempts"] == 1
    assert test_stage["status"] == "ok"
    assert test_stage["detail"] is None
    assert runner.commit  # checkout really happened on disk
