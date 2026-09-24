"""Unit tests for LLM model-node probing using httpx MockTransport."""

from __future__ import annotations

import httpx
import pytest

from app.dockwatch.config import ModelNode
from app.dockwatch.config import get_settings as dw_settings
from app.dockwatch.services import llm_service as llm


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    dw_settings.cache_clear()


def _patch_async_client(
    monkeypatch, handler, base_url: str = "https://", timeout: float = 5.0
) -> None:
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def factory(**kw):
        return real_async_client(
            base_url=kw.get("base_url", base_url),
            timeout=kw.get("timeout", timeout),
            transport=transport,
        )

    monkeypatch.setattr(llm.httpx, "AsyncClient", factory)


def _ollama_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/version":
        return httpx.Response(200, request=request, json={"version": "0.5.11"})
    if request.url.path == "/api/tags":
        return httpx.Response(
            200,
            request=request,
            json={
                "models": [
                    {"name": "llama3.1:8b", "size": 4700000000, "details": {"family": "llama"}},
                    {"name": "gemma2:2b", "size": None, "details": None},
                ]
            },
        )
    return httpx.Response(404, request=request)


def _llamacpp_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path in ("/health", "/v1/models"):
        return httpx.Response(
            200, request=request, json={"data": [{"id": "/models/tiny.gguf", "size": 123}]}
        )
    return httpx.Response(404, request=request)


@pytest.mark.asyncio
async def test_probe_ollama_node(monkeypatch) -> None:
    _patch_async_client(monkeypatch, _ollama_handler)
    node = ModelNode(name="local", url="http://ollama:11434", engine="ollama")
    result = await llm.probe_node(node)
    assert result["available"] is True
    assert result["version"] == "0.5.11"
    assert result["models"] == [
        {"name": "llama3.1:8b", "size": 4700000000, "family": "llama"},
        {"name": "gemma2:2b", "size": None, "family": None},
    ]


@pytest.mark.asyncio
async def test_probe_llamacpp_node(monkeypatch) -> None:
    _patch_async_client(monkeypatch, _llamacpp_handler)
    node = ModelNode(name="gguf", url="http://llama:8080", engine="llamacpp")
    result = await llm.probe_node(node)
    assert result["available"] is True
    assert result["version"] is None
    assert result["models"][0]["name"] == "tiny.gguf"
    assert result["models"][0]["path"] == "/models/tiny.gguf"
    assert result["models"][0]["family"] == "gguf"


@pytest.mark.asyncio
async def test_probe_node_lenient_dict_input(monkeypatch) -> None:
    _patch_async_client(monkeypatch, _ollama_handler)
    result = await llm.probe_node(
        {"name": "adhoc", "url": "http://ollama:11434", "engine": "OLLAMA"}
    )
    assert result["available"] is True


@pytest.mark.asyncio
async def test_probe_node_unsupported_engine() -> None:
    result = await llm.probe_node({"name": "weird", "url": "http://x", "engine": "transformers"})
    assert result["available"] is False
    assert "unsupported engine" in result["reason"]


@pytest.mark.asyncio
async def test_probe_node_unreachable(monkeypatch) -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_async_client(monkeypatch, _boom)
    node = ModelNode(name="down", url="http://127.0.0.1:1", engine="ollama")
    result = await llm.probe_node(node)
    assert result["available"] is False
    assert "connection refused" in result["reason"]
    assert result["models"] == []


@pytest.mark.asyncio
async def test_fleet_overview_aggregates(monkeypatch) -> None:
    async def fake_probe(node):
        return (
            {"available": True, "models": [1, 2]}
            if node.name == "a"
            else {"available": False, "models": []}
        )

    monkeypatch.setattr(llm, "probe_node", fake_probe)
    dw_settings.cache_clear()
    monkeypatch.setenv(
        "DOCKWATCH_MODEL_NODES",
        '[{"name":"a","url":"http://a:1","engine":"ollama"},'
        '{"name":"b","url":"http://b:1","engine":"ollama"}]',
    )
    dw_settings.cache_clear()
    overview = await llm.fleet_overview()
    assert overview["total_nodes"] == 2
    assert overview["reachable"] == 1
    assert overview["total_models"] == 2
