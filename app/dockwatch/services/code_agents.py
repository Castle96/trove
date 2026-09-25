"""Code-agent registration + git-server project enrollment helpers.

Seed time turns :data:`Settings.code_agents` into real DB rows so the Fleet
and Projects tabs reflect the ray / fleet / jarvis hosts:

* one ``kind="code"`` :class:`Endpoint` per host (``url`` = the model
  runtime), so the fleet overview probes reachability via the LLM node check;
* one ``kind="code"`` swarm :class:`Agent` linked to that endpoint, carrying
  ``{language, model, engine}`` in ``metadata_json``.

Repositories are intentionally NOT configured here: projects are enrolled on
the Projects tab (``repo_url`` pointing at your git server + assigned agent)
and workers pick up whatever repo is assigned to them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.config import CODE_AGENT_LANGUAGES, CodeAgentConfig
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.models.swarm import Agent

logger = logging.getLogger(__name__)


def _agent_metadata(cfg: CodeAgentConfig) -> str:
    return json.dumps(
        {
            "language": cfg.language,
            "model": cfg.model,
            "engine": cfg.engine,
            "repo_url": None,
            "branch": None,
        }
    )


async def ensure_code_agents(db: AsyncSession, configs: list[CodeAgentConfig]) -> list[Agent]:
    """Upsert fleet endpoints + swarm agents for every configured code agent.

    Idempotent: existing rows are looked up by name and kept (metadata/endpoint
    link refreshed). Returns the agent ORM objects ordered by name.
    """
    agents: list[Agent] = []
    for cfg in configs or []:
        if cfg.endpoint_url:
            endpoint = await db.scalar(select(Endpoint).where(Endpoint.name == cfg.name))
            if endpoint is None:
                endpoint = Endpoint(
                    name=cfg.name,
                    url=cfg.endpoint_url,
                    kind="code",
                    enabled=True,
                    description=f"{cfg.language} smol-agent host (model: {cfg.model})",
                )
                db.add(endpoint)
                await db.flush()
                logger.info("registered code endpoint %s (kind=code)", cfg.name)
            else:
                endpoint.kind = "code"
                endpoint.url = cfg.endpoint_url
                endpoint.enabled = True
                endpoint.description = f"{cfg.language} smol-agent host (model: {cfg.model})"
        else:
            endpoint = None

        agent = await db.scalar(select(Agent).where(Agent.name == cfg.name))
        if agent is None:
            agent = Agent(
                name=cfg.name,
                kind="code",
                status="idle",
                endpoint_id=endpoint.id if endpoint is not None else None,
                metadata_json=_agent_metadata(cfg),
            )
            db.add(agent)
            await db.flush()
            logger.info("registered code agent %s (language=%s)", cfg.name, cfg.language)
        else:
            agent.kind = "code"
            agent.endpoint_id = endpoint.id if endpoint is not None else agent.endpoint_id
            agent.metadata_json = _agent_metadata(cfg)
            if agent.status not in ("working", "needs_approval"):
                agent.status = "idle"
        agents.append(agent)
    await db.commit()
    for agent in agents:
        await db.refresh(agent)
    return agents


async def fetch_code_agents(db: AsyncSession) -> list[Agent]:
    """All ``kind="code"`` swarm agents ordered by name."""
    rows = await db.scalars(select(Agent).where(Agent.kind == "code").order_by(Agent.name))
    return list(rows.all())


def validate_language(language: str) -> bool:
    """Whether *language* is a supported code-agent target."""
    return language in CODE_AGENT_LANGUAGES


def is_agent_matched(agent: Agent, language: str | None = None) -> bool:
    """Filter predicate: a code agent for *language* (or any)."""
    if agent.kind != "code":
        return False
    if language is None:
        return True
    try:
        meta = json.loads(agent.metadata_json or "{}")
    except Exception:
        meta = {}
    return str(meta.get("language", "")) == language


def endpoint_names_by_id(agents: list[Any], endpoints: list[Any]) -> dict[int, str]:
    """Map endpoint_id -> name for the agents in *agents*."""
    if not agents or not endpoints:
        return {}
    ids = {a.endpoint_id for a in agents if a.endpoint_id is not None}
    return {int(e.id): str(e.name) for e in endpoints if e.id in ids}
