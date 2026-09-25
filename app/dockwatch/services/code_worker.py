"""Code-agent worker ("code-agent" console script).

A language-specialized development agent that runs on a tailnet host next to
its smol model runtime (Ollama / llama.cpp), driving real dev pipelines for
the repository enrolled to it on the Projects tab and reporting per-stage
telemetry back to trove.

Loop (every ``--interval`` seconds):

1. Heartbeat the swarm Agent (``POST /api/swarm/agents/{id}/heartbeat``).
2. Pull the agent config (language/model) from its swarm metadata.
3. Find the enrolled project assigned to this agent (swarm Project with
   ``repo_url`` + ``agent_id`` == self) — the repo comes from your git server.
4. If the project's HEAD commit already has a finished run, idle and wait.
5. Post a ``running`` ingest, then run the language's stage commands:
   ``checkout → deps → format → lint → build → test → report`` measuring each.
6. On a failing lint/build/test stage, optionally let a local smolagents
   ``CodeAgent`` (driven by the host's model) attempt fixes and re-run up to
   ``--fix-depth`` times.
7. Post the final ``ok``/``error`` ingest with full stage timings.

Plain-pipeline mode (``--fix-depth 0``) never touches the model, so the worker
runs anywhere. The agentic fix loop imports ``smolagents`` lazily — install
with ``uv sync --extra agents``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("code-agent")

#: Canonical stage order (matches ``models/pipeline.py``).
STAGES = ("checkout", "deps", "format", "lint", "build", "test", "report")

#: Per-language stage commands (argv lists, run with ``cwd`` = checkout dir).
LANGUAGE_STAGES: dict[str, dict[str, list[str]]] = {
    "rust": {
        "deps": ["cargo", "fetch"],
        "format": ["cargo", "fmt", "--check"],
        "lint": ["cargo", "clippy", "--quiet"],
        "build": ["cargo", "build", "--quiet"],
        "test": ["cargo", "test", "--quiet"],
        "report": ["git", "log", "-1", "--format=%s"],
    },
    "go": {
        "deps": ["go", "mod", "download"],
        "format": ["bash", "-c", "! gofmt -l . | grep ."],
        "lint": ["go", "vet", "./..."],
        "build": ["go", "build", "./..."],
        "test": ["go", "test", "./..."],
        "report": ["git", "log", "-1", "--format=%s"],
    },
    "python": {
        "deps": ["python3", "-m", "pip", "install", "-e", "."],
        "format": ["python3", "-m", "ruff", "format", "--check", "."],
        "lint": ["python3", "-m", "ruff", "check", "."],
        "build": ["python3", "-m", "compileall", "-q", "."],
        "test": ["python3", "-m", "pytest", "-q"],
        "report": ["git", "log", "-1", "--format=%s"],
    },
}


@dataclass
class WorkerConfig:
    trove_url: str
    agent_name: str
    api_key: str | None = None
    ollama_url: str | None = None
    model: str | None = None
    workspace: str = "work"
    interval: float = 60.0
    fix_depth: int = 0
    timeout: float = 600.0
    mark_project_done: bool = False
    once: bool = False
    force: bool = False

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> WorkerConfig:
        return cls(
            trove_url=(args.trove_url or os.environ.get("TROVE_URL", "")).rstrip("/"),
            agent_name=args.agent_name or os.environ.get("CODE_AGENT_NAME", ""),
            api_key=args.api_key or os.environ.get("TROVE_API_KEY"),
            ollama_url=(args.ollama_url or os.environ.get("OLLAMA_BASE_URL", "")).rstrip("/")
            or None,
            model=args.model or os.environ.get("CODE_AGENT_MODEL"),
            workspace=args.workspace or os.environ.get("CODE_WORKER_WORKSPACE", "work"),
            interval=args.interval,
            fix_depth=args.fix_depth,
            timeout=args.timeout,
            mark_project_done=args.mark_project_done,
            once=args.once,
            force=args.force,
        )


class TroveClient:
    """Thin httpx client for the trove control-plane API."""

    def __init__(self, cfg: WorkerConfig) -> None:
        headers = {}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._client = httpx.Client(base_url=cfg.trove_url, headers=headers, timeout=30.0)
        self.agent_name = cfg.agent_name
        self._agent_id: int | None = None

    def _get(self, path: str, **kw: Any) -> Any:
        resp = self._client.get(path, **kw)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, **kw: Any) -> Any:
        resp = self._client.post(path, **kw)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, **kw: Any) -> None:
        resp = self._client.patch(path, **kw)
        resp.raise_for_status()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------- swarm
    def resolve_agent(self) -> dict[str, Any]:
        agents = self._get("/api/swarm")
        for agent in agents:
            if agent.get("name") == self.agent_name:
                self._agent_id = int(agent["id"])
                return agent
        raise RuntimeError(f"agent {self.agent_name!r} not registered on {self.trove_url}")

    def heartbeat(self, status: str, snippet: str | None = None) -> None:
        if self._agent_id is None:
            return
        payload: dict[str, Any] = {"status": status}
        if snippet:
            payload["conversation_snippet"] = snippet[:500]
        self._post(f"/api/swarm/agents/{self._agent_id}/heartbeat", json=payload)

    def assigned_projects(self) -> list[dict[str, Any]]:
        if self._agent_id is None:
            return []
        projects = self._get("/api/swarm/projects")
        out = []
        for project in projects:
            if project.get("agent_id") == self._agent_id and project.get("repo_url"):
                out.append(project)
        return out

    def latest_run_for(self, project_id: int) -> dict[str, Any] | None:
        runs = self._get("/api/pipeline/runs", params={"limit": 20})
        for run in runs:
            if run.get("project_id") == project_id:
                return run
        return None

    # ----------------------------------------------------------- pipeline
    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/pipeline/ingest", json=payload)


def _run(
    cmd: list[str], cwd: str, timeout: float, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a stage command; returns ``(returncode, stdout, stderr)``."""
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged_env,
    )
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def _git(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    return _run(["git", *cmd], cwd, timeout)


def _current_commit(repo_dir: str, timeout: float) -> str | None:
    code, out, _ = _git(["rev-parse", "HEAD"], repo_dir, timeout)
    return out.strip() if code == 0 else None


def _tail(text: str, limit: int = 2000) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    return text[-limit:]


def _stage_error_detail(stderr: str, stdout: str) -> str | None:
    pieces = [p for p in (stderr.strip(), stdout.strip()) if p]
    return (" | ".join(pieces))[-500:]


class RepoRunner:
    """A live git checkout of the enrolled project."""

    def __init__(self, cfg: WorkerConfig, repo_url: str, branch: str, name: str) -> None:
        self.cfg = cfg
        self.repo_url = repo_url
        self.branch = branch or "main"
        self.dir = os.path.join(cfg.workspace, f"{name}-{self.branch.replace('/', '_')}")
        self.commit: str | None = None
        self.language = ""

    # ------------------------------------------------------------- lifecycle
    def checkout(self) -> tuple[str | None, str | None, float]:
        """Clone/update the repo; returns (commit, error, latency_ms)."""
        start = time.monotonic()
        if not os.path.isdir(os.path.join(self.dir, ".git")):
            os.makedirs(self.cfg.workspace, exist_ok=True)
            code, out, err = _git(
                [
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--branch",
                    self.branch,
                    self.repo_url,
                    self.dir,
                ],
                self.cfg.workspace,
                self.cfg.timeout,
            )
            if code != 0:
                return None, _stage_error_detail(err, out), int((time.monotonic() - start) * 1000)
        else:
            code, out, err = _git(["fetch", "origin", self.branch], self.dir, self.cfg.timeout)
            if code != 0:
                return None, _stage_error_detail(err, out), int((time.monotonic() - start) * 1000)
            _git(["checkout", "--quiet", self.branch], self.dir, self.cfg.timeout)
            _git(["pull", "--quiet", "--ff-only"], self.dir, self.cfg.timeout)
        self.commit = _current_commit(self.dir, self.cfg.timeout)
        return self.commit, None, int((time.monotonic() - start) * 1000)

    # --------------------------------------------------------------- stages
    def run_stage(self, stage: str) -> tuple[str, str | None, int]:
        """Execute one stage; returns (status, detail, latency_ms)."""
        cmd = LANGUAGE_STAGES.get(self.language, {}).get(stage)
        if stage == "report" and cmd is None:
            code, out, err = _git(["log", "-1", "--format=%s"], self.dir, self.cfg.timeout)
            if code == 0:
                return "ok", (out.strip() or None), 0
        if cmd is None:
            return "skipped", None, 0
        start = time.monotonic()
        try:
            code, out, err = _run(cmd, self.dir, self.cfg.timeout)
        except subprocess.TimeoutExpired:
            return (
                "error",
                f"stage timed out after {self.cfg.timeout:.0f}s",
                int((time.monotonic() - start) * 1000),
            )
        latency = int((time.monotonic() - start) * 1000)
        detail = _stage_error_detail(err, out) if code != 0 else None
        return ("ok" if code == 0 else "error"), detail, latency

    def attempt_fix(self, stage: str, detail: str) -> str:
        """Ask the local smol CodeAgent to fix the failing stage; returns status."""
        if not self.cfg.ollama_url or not self.cfg.model:
            return "error"
        try:
            from smolagents import (  # type: ignore[import-not-found]
                CodeAgent,
                OpenAIServerModel,
                Tool,  # type: ignore[import-not-found]
            )
        except Exception as exc:  # pragma: no cover - extra not installed
            logger.warning("smolagents unavailable (%s); fix loop disabled", exc)
            return "error"

        class RunShell(Tool):
            name = "run_shell"
            description = (
                "Run a shell command in the repository working directory and get its stdout+stderr."
            )
            inputs = {"command": {"type": "string", "description": "the shell command to run"}}
            output_type = "string"

            def forward(self, command: str) -> str:
                code, out, err = _run(
                    ["bash", "-lc", command],
                    self.dir,
                    self.cfg.timeout,
                    env={"PATH": os.environ.get("PATH", "")},
                )
                return f"exit={code}\nstdout:\n{out}\nstderr:\n{err}"

        class ReadFile(Tool):
            name = "read_file"
            description = "Read a file from the repository."
            inputs = {"path": {"type": "string", "description": "repo-relative file path"}}
            output_type = "string"

            def forward(self, path: str) -> str:
                try:
                    with open(os.path.join(self.dir, path), encoding="utf-8") as fh:
                        return fh.read()
                except Exception as exc:
                    return f"error: {exc}"

        class WriteFile(Tool):
            name = "write_file"
            description = "Overwrite a file in the repository."
            inputs = {
                "path": {"type": "string", "description": "repo-relative file path"},
                "content": {"type": "string", "description": "new file content"},
            }
            output_type = "string"

            def forward(self, path: str, content: str) -> str:
                full = os.path.join(self.dir, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(content)
                return "written"

        stage_cmd = " ".join(LANGUAGE_STAGES.get(self.language, {}).get(stage, ["true"]))
        task = (
            f"I am fixing a failing '{stage}' stage for a {self.language} project at {self.dir}.\n"
            f"The failing check is: `{stage_cmd}`\nFailure:\n{detail[:2000]}\n"
            f"Make the smallest change that makes `{stage_cmd}` pass. Re-run it with run_shell to "
            f"verify before finishing. Preserve behavior and public APIs."
        )
        try:
            model = OpenAIServerModel(model_id=self.cfg.model, api_base=self.cfg.ollama_url)
            agent = CodeAgent(
                tools=[RunShell(), ReadFile(), WriteFile()],
                model=model,
                max_steps=6,
                additional_authorized_imports=["os", "subprocess"],
            )
            agent.run(task)
        except Exception as exc:
            logger.warning("agentic fix failed for stage %s: %s", stage, exc)
            return "error"
        code, out, err = _run(stage_cmd.split(), self.dir, self.cfg.timeout)
        return "ok" if code == 0 else "error"


# ------------------------------------------------------------------ loop
def build_pipeline_stages(
    runner: RepoRunner, cfg: WorkerConfig, checkout_done: bool = False
) -> list[dict[str, Any]]:
    """Run the full stage list, returning ingest-ready stage records.

    A failing stage short-circuits the remaining stages (they become
    ``skipped``) unless the fix loop succeeds and the pipeline continues.
    """
    records: list[dict[str, Any]] = []

    def record(stage: str, status: str, latency: int, detail: str | None = None) -> None:
        records.append(
            {
                "stage": stage,
                "status": status,
                "latency_ms": latency,
                "detail": detail,
                "output": None,
            }
        )

    for stage in STAGES:
        if stage == "checkout":
            if checkout_done and runner.commit:
                record(stage, "ok", 0)
                continue
            status, detail, latency = runner.checkout()
            record(stage, "ok" if status else "error", latency, detail)
            if not status:
                return records
            continue
        status, detail, latency = runner.run_stage(stage)
        if status == "error" and cfg.fix_depth > 0 and stage in ("format", "lint", "build", "test"):
            for _ in range(cfg.fix_depth):
                fixed = runner.attempt_fix(stage, detail or "")
                if fixed == "ok":
                    status = "ok"
                    detail = None
                    break
        record(stage, status, latency, detail)
        if status == "error":
            for rest in STAGES[STAGES.index(stage) + 1 :]:
                record(rest, "skipped", 0)
            break
    return records


def run_once(cfg: WorkerConfig) -> str:
    """One poll cycle. Returns a short summary string (for logging/testing)."""
    client = TroveClient(cfg)
    try:
        agent = client.resolve_agent()
        meta = json.loads(agent.get("metadata_json") or "{}")
        language = str(meta.get("language", ""))
        if not language:
            logger.warning("agent %s has no language; skipping", cfg.agent_name)
            return "no-language"
        projects = client.assigned_projects()
        if not projects:
            logger.info("agent %s: no enrolled project yet (idle)", cfg.agent_name)
            return "idle"
        project = projects[0]
        project_id = int(project["id"])
        repo_url = str(project["repo_url"])
        branch = str(project.get("branch") or "main")
        repo_name = os.path.splitext(os.path.basename(repo_url.rstrip("/")))[0] or "project"

        client.heartbeat("working", f"pipeline → {repo_name}:{branch}")
        runner = RepoRunner(cfg, repo_url, branch, repo_name)
        runner.language = language
        commit, err, _ = runner.checkout()
        if commit is None:
            client.heartbeat("idle", f"checkout failed: {(err or 'git error')[:200]}")
            return f"checkout-failed: {repo_name}"

        if not cfg.force:
            latest = client.latest_run_for(project_id)
            if (
                latest
                and latest.get("status") in ("ok", "error")
                and latest.get("commit") == commit
            ):
                client.heartbeat("idle", f"unchanged @ {commit[:8]} ({repo_name}:{branch})")
                return "unchanged"

        client.ingest(
            {
                "agent_name": cfg.agent_name,
                "project_id": project_id,
                "language": language,
                "repo_url": repo_url,
                "branch": branch,
                "commit": commit,
                "status": "running",
                "stages": [],
            }
        )

        stages = build_pipeline_stages(runner, cfg, checkout_done=True)
        final_status = "ok" if not any(s["status"] == "error" for s in stages) else "error"
        client.ingest(
            {
                "agent_name": cfg.agent_name,
                "project_id": project_id,
                "language": language,
                "repo_url": repo_url,
                "branch": branch,
                "commit": runner.commit,
                "status": final_status,
                "stages": [{k: v for k, v in s.items() if k != "output"} for s in stages],
            }
        )
        if cfg.mark_project_done:
            try:
                client._patch(
                    f"/api/swarm/projects/{project_id}",
                    json={"status": "done" if final_status == "ok" else "blocked"},
                )
            except Exception:
                logger.debug("could not update project status (read-only worker?)", exc_info=True)
        snippet = f"{final_status} @ {commit[:8]} ({repo_name}:{branch})"
        client.heartbeat("idle", snippet)
        return snippet
    finally:
        client.close()


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-agent",
        description="Language-specialized dev-pipeline worker reporting to trove.",
    )
    parser.add_argument(
        "--trove-url", default=os.environ.get("TROVE_URL"), help="trove base URL (env TROVE_URL)"
    )
    parser.add_argument(
        "--agent-name",
        default=os.environ.get("CODE_AGENT_NAME"),
        help="swarm agent name (env CODE_AGENT_NAME)",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("TROVE_API_KEY"), help="trove API key, if auth is on"
    )
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_BASE_URL"),
        help="host model runtime base URL (env OLLAMA_BASE_URL)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODE_AGENT_MODEL"),
        help="model id used by the agentic fix loop",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("CODE_WORKER_WORKSPACE", "work"),
        help="directory to clone repos into",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("CODE_WORKER_INTERVAL", "60")),
        help="poll interval seconds",
    )
    parser.add_argument(
        "--fix-depth",
        type=int,
        default=int(os.environ.get("CODE_AGENT_FIX_DEPTH", "0")),
        help="agentic fix iterations per failing stage (0 = plain pipeline)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("CODE_WORKER_TIMEOUT", "600")),
        help="per-command timeout seconds",
    )
    parser.add_argument(
        "--mark-project-done",
        action="store_true",
        default=False,
        help="move the enrolled project to done/blocked after each run",
    )
    parser.add_argument(
        "--once", action="store_true", default=False, help="run a single poll cycle and exit"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="re-run the pipeline even when HEAD is unchanged",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("CODE_AGENT_LOG_LEVEL", "INFO"), help="logging level"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = WorkerConfig.from_env(args)
    if not cfg.trove_url or not cfg.agent_name:
        print(
            "code-agent requires --trove-url and --agent-name (or TROVE_URL / CODE_AGENT_NAME)",
            file=sys.stderr,
        )
        return 2
    if cfg.fix_depth > 0 and (not cfg.ollama_url or not cfg.model):
        logger.warning(
            "fix-depth > 0 but no OLLAMA_BASE_URL/CODE_AGENT_MODEL set; fix loop disabled"
        )

    try:
        if cfg.once:
            print(run_once(cfg))
            return 0
        while True:
            try:
                print(run_once(cfg))
            except Exception as exc:
                logger.warning("poll cycle failed: %s", exc)
            time.sleep(cfg.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
