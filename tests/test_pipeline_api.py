"""API tests for dev pipelines: ingest, live, overview, run history.

Uses the ``dw_db`` fixture (fresh temp Dockwatch tables) and seeds the code
agents via ``ensure_code_agents`` the same way app startup does.
"""

from __future__ import annotations

import pytest_asyncio

from app.dockwatch.config import CodeAgentConfig
from app.dockwatch.database import get_session_factory
from app.dockwatch.services.code_agents import ensure_code_agents

STAGES_OK = [
    {"stage": s, "status": "ok", "latency_ms": i * 10, "detail": None}
    for i, s in enumerate(("checkout", "deps", "format", "lint", "build", "test", "report"))
]


@pytest_asyncio.fixture
async def ray_agent(dw_db):
    """Register a ``ray`` (rust) code agent exactly like startup does."""
    from app.dockwatch.config import get_settings as dw_settings

    dw_settings.cache_clear()
    async with get_session_factory()() as db:
        agents = await ensure_code_agents(
            db,
            [
                CodeAgentConfig(
                    name="ray",
                    language="rust",
                    model="qwen2.5-coder:1.5b",
                    endpoint_url="http://ray:11434",
                )
            ],
        )
    return agents[0]


async def test_pipeline_ingest_running_then_ok(client, ray_agent) -> None:
    res = await client.post(
        "/api/pipeline/ingest",
        json={
            "agent_name": "ray",
            "project_id": None,
            "language": "rust",
            "repo_url": "git@git.example.net:team/tool.git",
            "branch": "main",
            "commit": "abc123",
            "status": "running",
            "stages": [],
        },
    )
    assert res.status_code == 201, res.text
    run_id = res.json()["id"]

    ok = await client.post(
        "/api/pipeline/ingest",
        json={
            "agent_name": "ray",
            "project_id": None,
            "language": "rust",
            "repo_url": "git@git.example.net:team/tool.git",
            "branch": "main",
            "commit": "abc123",
            "status": "ok",
            "stages": STAGES_OK,
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["status"] == "ok"

    live = (await client.get("/api/pipeline/live")).json()
    assert len(live) == 1
    entry = live[0]
    assert entry["agent_name"] == "ray"
    assert entry["language"] == "rust"
    assert entry["model"] == "qwen2.5-coder:1.5b"
    assert entry["last_run_status"] == "ok"  # newest run wins
    assert entry["last_commit"] == "abc123"
    assert entry["repo_url"] == "git@git.example.net:team/tool.git"
    assert [s["stage"] for s in entry["stages"]] == [
        "checkout",
        "deps",
        "format",
        "lint",
        "build",
        "test",
        "report",
    ]
    assert entry["stages"][2]["latency_ms"] == 20

    overview = (await client.get("/api/pipeline/overview")).json()
    assert overview["total_runs"] == 2
    assert overview["ok_runs"] == 1
    assert overview["running"] == 1
    assert overview["agents"] == ["ray"]

    runs = (await client.get("/api/pipeline/runs")).json()
    assert len(runs) == 2
    assert runs[0]["status"] == "ok"  # newest first
    assert runs[0]["commit"] == "abc123"

    detail = (await client.get(f"/api/pipeline/runs/{run_id}")).json()
    assert detail["id"] == run_id
    assert detail["status"] == "running"

    filtered = (await client.get("/api/pipeline/runs", params={"agent_id": ray_agent.id})).json()
    assert len(filtered) == 2


async def test_pipeline_ingest_error_short_circuits_stages(client, ray_agent) -> None:
    res = await client.post(
        "/api/pipeline/ingest",
        json={
            "agent_name": "ray",
            "status": "error",
            "stages": [
                {"stage": "checkout", "status": "ok", "latency_ms": 5},
                {
                    "stage": "deps",
                    "status": "error",
                    "latency_ms": 30,
                    "detail": "cargo fetch: net fail",
                },
                {"stage": "format", "status": "skipped", "latency_ms": 0},
            ],
        },
    )
    assert res.status_code == 201, res.text

    live = (await client.get("/api/pipeline/live")).json()
    assert live[0]["last_run_status"] == "error"
    stages = {s["stage"]: s for s in live[0]["stages"]}
    assert stages["deps"]["status"] == "error"
    assert stages["deps"]["detail"] == "cargo fetch: net fail"


async def test_pipeline_ingest_unknown_agent_404(client) -> None:
    res = await client.post("/api/pipeline/ingest", json={"agent_name": "nope", "stages": []})
    assert res.status_code == 404


async def test_pipeline_ingest_validates_project(client, ray_agent) -> None:
    created = (
        await client.post(
            "/api/swarm/projects",
            json={
                "name": "tool",
                "repo_url": "git@git.example.net:team/tool.git",
                "branch": "main",
            },
        )
    ).json()

    res = await client.post(
        "/api/pipeline/ingest",
        json={
            "agent_name": "ray",
            "project_id": created["id"],
            "status": "ok",
            "stages": STAGES_OK,
        },
    )
    assert res.status_code == 201, res.text
    run = (await client.get(f"/api/pipeline/runs/{res.json()['id']}")).json()
    assert run["project_id"] == created["id"]

    bad = await client.post(
        "/api/pipeline/ingest",
        json={"agent_name": "ray", "project_id": 9999, "stages": []},
    )
    assert bad.status_code == 404


def stub_no_auto(monkeypatch) -> None:
    """Skip the fire-and-forget discovery task on endpoint create."""
    from app.dockwatch.api import endpoints as ep_mod

    monkeypatch.setattr(ep_mod, "_queue_discovery", lambda endpoint_id: None)


async def test_code_endpoint_probe(client, dw_db, monkeypatch) -> None:
    """kind=code endpoints probe the model runtime (Ollama tags), not Docker."""
    stub_no_auto(monkeypatch)
    from app.dockwatch.services import llm_service

    async def fake_probe(node):
        return {
            "name": str(node["name"]),
            "engine": "ollama",
            "url": str(node["url"]),
            "available": True,
            "reason": None,
            "version": "0.3.1",
            "models": [{"name": "qwen2.5-coder:1.5b", "size": 900_000_000, "family": "qwen2"}],
        }

    monkeypatch.setattr(llm_service, "probe_node", fake_probe)

    endpoint = (
        await client.post(
            "/api/endpoints",
            json={"name": "ray", "url": "http://ray:11434", "kind": "code", "enabled": True},
        )
    ).json()

    res = await client.post(f"/api/endpoints/{endpoint['id']}/test")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["available"] is True
    assert body["engine"] == "ollama"
    assert body["version"] == "0.3.1"
    assert body["links_discovered"] == 0  # model runtimes have no published ports

    overview = (await client.get("/api/endpoints/fleet/overview")).json()
    entry = next(e for e in overview["endpoints"] if e["endpoint_id"] == endpoint["id"])
    assert entry["kind"] == "code"
    assert entry["available"] is True

    listed = (await client.get("/api/endpoints")).json()
    assert any(e["id"] == endpoint["id"] and e["kind"] == "code" for e in listed)


async def test_code_endpoint_probe_down(client, dw_db, monkeypatch) -> None:
    stub_no_auto(monkeypatch)
    from app.dockwatch.services import llm_service

    async def fake_probe(node):
        return {
            "name": str(node["name"]),
            "engine": "ollama",
            "url": str(node["url"]),
            "available": False,
            "reason": "unreachable",
            "version": None,
            "models": [],
        }

    monkeypatch.setattr(llm_service, "probe_node", fake_probe)

    endpoint = (
        await client.post(
            "/api/endpoints",
            json={"name": "ray", "url": "http://ray:11434", "kind": "code", "enabled": True},
        )
    ).json()
    res = await client.post(f"/api/endpoints/{endpoint['id']}/test")
    assert res.status_code == 200, res.text
    assert res.json()["available"] is False
    assert res.json()["reason"] == "unreachable"


async def test_ensure_code_agents_idempotent_dw_db(dw_db, ray_agent) -> None:
    """Startup re-seeding doesn't duplicate endpoints or agents."""
    from app.dockwatch.services.code_agents import fetch_code_agents

    async with get_session_factory()() as db:
        again = await ensure_code_agents(
            db,
            [
                CodeAgentConfig(
                    name="ray",
                    language="rust",
                    model="qwen2.5-coder:1.5b",
                    endpoint_url="http://ray:11434",
                )
            ],
        )
        assert again[0].id == ray_agent.id
        agents = await fetch_code_agents(db)
        assert len(agents) == 1
        assert agents[0].id == ray_agent.id


async def test_pipeline_runs_filter_unknown_agent_empty(client, ray_agent) -> None:
    assert (await client.get("/api/pipeline/runs", params={"agent_id": 999})).json() == []
    assert (await client.get("/api/pipeline/overview")).json()["total_runs"] == 0


async def test_pipeline_live_empty_without_code_agents(client, dw_db) -> None:
    assert (await client.get("/api/pipeline/live")).json() == []
    assert (await client.get("/api/pipeline/overview")).json()["agents"] == []
