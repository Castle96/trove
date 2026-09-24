"""ORM models for agent-swarm tasks and shared conversation threads.

Depends on `Agent` and `Project` already being mapped; imported after the
core swarm models in ``app/models/__init__.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.dockwatch.database import Base
from app.dockwatch.models.inventory import TimestampMixin

if TYPE_CHECKING:
    from app.dockwatch.models.swarm import Agent, Project


class ConversationMessage(Base, TimestampMixin):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)

    project: Mapped[Project] = relationship("Project", back_populates="messages")
    agent: Mapped[Agent | None] = relationship("Agent", back_populates="messages")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="todo")
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    project: Mapped[Project] = relationship("Project", back_populates="tasks")
    agent: Mapped[Agent | None] = relationship("Agent", back_populates="tasks")

    result_summary: Mapped[str | None] = mapped_column(Text)

    def mark_in_progress(self, agent_id: int) -> None:
        self.status = "in_progress"
        self.agent_id = agent_id

    def mark_done(self) -> None:
        self.status = "done"
        self.agent_id = None

    def mark_blocked(self) -> None:
        self.status = "blocked"

    def mark_todo(self) -> None:
        self.status = "todo"
        self.agent_id = None
