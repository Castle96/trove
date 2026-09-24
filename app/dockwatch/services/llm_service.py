"""LLM runtime integration service.

Polls the per-node smol-model runtimes (Ollama and llama.cpp ``llama-server``)
that both speak overlapping HTTP APIs. Fan-out is concurrent with bounded
parallelism; a node being down never fails the whole view — each result
carries its own ``available`` flag (same contract as ``DockerEndpointManager``).

Configure model nodes via the ``DOCKWATCH_MODEL_NODES`` environment variable
(see ``.env.example`` and the Model Node settings in ``app/dockwatch/config.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from app.dockwatch.config import ModelNode, get_settings

logger = logging.getLogger(__name__)

#: Node engines this service knows how to talk to.
_SUPPORTED_ENGINES = {"ollama", "llamacpp"}


def _model_entry(name: str, size: int | None = None, family: str | None = None) -> dict[str, Any]:
    """Normalize a model record across engines."""
    return {"name": name, "size": size, "family": family}


async def _probe_ollama(client: httpx.AsyncClient) -> tuple[str | None, list[dict[str, Any]]]:
    """Return ``(version, models)`` from an Ollama server."""
    version_resp = await client.get("/api/version")
    version_resp.raise_for_status()
    version = version_resp.json().get("version")

    tags_resp = await client.get("/api/tags")
    tags_resp.raise_for_status()
    models = [
        _model_entry(
            name=str(m.get("name", "")),
            size=int(m["size"]) if m.get("size") is not None else None,
            family=(m.get("details") or {}).get("family"),
        )
        for m in tags_resp.json().get("models", [])
    ]
    return version, models


async def _probe_llamacpp(client: httpx.AsyncClient) -> tuple[str | None, list[dict[str, Any]]]:
    """Return ``(version, models)`` from a llama.cpp ``llama-server``.

    ``/v1/models`` is the OpenAI-compatible listing; ``/health`` is a cheap
    liveness signal. The server version is not exposed by the API, so it is
    reported as the engine build when available.
    """
    await client.get("/health")
    resp = await client.get("/v1/models")
    resp.raise_for_status()
    models = []
    for m in resp.json().get("data", []):
        raw = str(m.get("id", ""))
        # llama-server reports the on-disk path; display the basename but keep
        # the full path available for tooltips / API consumers.
        models.append(
            {
                **_model_entry(
                    name=os.path.basename(raw) or raw,
                    size=int(m["size"]) if m.get("size") not in (None, "") else None,
                    family=m.get("owned_by") or "gguf",
                ),
                "path": raw,
            }
        )
    return None, models


async def probe_node(node: ModelNode | dict[str, Any]) -> dict[str, Any]:
    """Poll one model node; always returns a dict (never raises).

    Accepts a validated :class:`ModelNode` (from settings) or a raw mapping
    for tests/back-compat; the mapping path is lenient like the old code.
    """
    settings = get_settings()
    if isinstance(node, ModelNode):
        name = node.name
        url = node.url
        engine = str(node.engine)
    else:
        name = str(node.get("name", "unknown"))
        url = str(node.get("url", "")).rstrip("/")
        engine = str(node.get("engine", "ollama")).lower()
    if engine not in _SUPPORTED_ENGINES:
        return {
            "name": name,
            "engine": engine,
            "url": url,
            "available": False,
            "reason": f"unsupported engine: {engine}",
            "version": None,
            "models": [],
        }
    try:
        async with httpx.AsyncClient(base_url=url, timeout=settings.model_poll_timeout) as client:
            if engine == "ollama":
                version, models = await asyncio.wait_for(
                    _probe_ollama(client), settings.model_poll_timeout
                )
            else:
                version, models = await asyncio.wait_for(
                    _probe_llamacpp(client), settings.model_poll_timeout
                )
    except Exception as exc:
        logger.warning("model node %s (%s) unreachable: %s", name, url, exc)
        return {
            "name": name,
            "engine": engine,
            "url": url,
            "available": False,
            "reason": str(exc),
            "version": None,
            "models": [],
        }
    return {
        "name": name,
        "engine": engine,
        "url": url,
        "available": True,
        "reason": None,
        "version": version,
        "models": models,
    }


async def fleet_overview() -> dict[str, Any]:
    """Poll every configured model node concurrently (partial failures OK)."""
    settings = get_settings()
    nodes = list(settings.model_nodes)
    sem = asyncio.Semaphore(max(settings.model_poll_concurrency, 1))

    async def one(node: ModelNode) -> dict[str, Any]:
        async with sem:
            return await probe_node(node)

    results = list(await asyncio.gather(*(one(n) for n in nodes)))
    reachable = sum(1 for r in results if r.get("available"))
    return {
        "nodes": results,
        "total_nodes": len(results),
        "reachable": reachable,
        "total_models": sum(len(r.get("models", [])) for r in results),
    }
