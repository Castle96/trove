"""SQLAlchemy models for the agent-swarm dashboard.

Agents are long-lived workers (Claude / Codex / Orca, ...) that check in
via heartbeats. Projects are units of work that can be assigned to an
agent and may require human approval. SwarmNotifications surface events
(approvals, warnings, errors) for the dashboard feed.

ConversationMessage and Task live in ``app.models.swarm_tasks``; their
relationships on Agent/Project are configured with string targets so that
SQLAlchemy can resolve them regardless of import order.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.dockwatch.database import Base
from app.dockwatch.models.inventory import TimestampMixin, utcnow

if TYPE_CHECKING:
    from app.dockwatch.models.swarm_tasks import ConversationMessage, Task


class Agent(Base, TimestampMixin):
    """A worker agent that heartbeats into the swarm."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    #: e.g. ``claude`` | ``codex`` | ``orca``.
    kind: Mapped[str] = mapped_column(String(30), default="agent", nullable=False)
    endpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("endpoints.id", ondelete="SET NULL"), index=True
    )
    #: ``idle`` | ``working`` | ``needs_approval`` | ``offline``.
    status: Mapped[str] = mapped_column(String(30), default="idle", nullable=False)
    last_heartbeat: Mapped[datetime | None] = mapped_column()
    #: FK added later once projects exist; plain int for now to avoid circularity.
    current_project_id: Mapped[int | None] = mapped_column()
    last_conversation_snippet: Mapped[str | None] = mapped_column(Text)
    #: JSON string with free-form agent metadata.
    metadata_json: Mapped[str | None] = mapped_column(Text)

    messages: Mapped[list[ConversationMessage]] = relationship(
        "ConversationMessage", back_populates="agent", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[Task]] = relationship(
        "Task", back_populates="agent", cascade="all, delete-orphan"
    )

    def touch_heartbeat(self, status: str | None = None) -> None:
        """Record a heartbeat, optionally transitioning status."""
        if status is not None:
            self.status = status
        self.last_heartbeat = utcnow()


class Project(Base, TimestampMixin):
    """A unit of work tracked on the swarm dashboard."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: ``queued`` | ``active`` | ``needs_approval`` | ``done`` | ``blocked``.
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    repo_url: Mapped[str | None] = mapped_column(String(500))
    branch: Mapped[str | None] = mapped_column(String(200))
    conversation_id: Mapped[str | None] = mapped_column(String(200), index=True)
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approval_note: Mapped[str | None] = mapped_column(Text)

    messages: Mapped[list[ConversationMessage]] = relationship(
        "ConversationMessage", back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[Task]] = relationship(
        "Task", back_populates="project", cascade="all, delete-orphan"
    )


class SwarmNotification(Base, TimestampMixin):
    """A dashboard feed event, optionally linked to a project/agent."""

    __tablename__ = "swarm_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: ``info`` | ``warning`` | ``approval`` | ``error``.
    level: Mapped[str] = mapped_column(String(30), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


def _validate_task_status(status: str) -> str:
    """Return *status* if it is a recognized task state, else raise."""
    if status not in ("todo", "in_progress", "done", "blocked"):
        raise ValueError(f"Unknown task status: {status!r}")
    return status
