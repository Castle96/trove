"""Pydantic schemas for the agent-swarm dashboard.

Includes agents, projects, notifications, plus tasks and conversation
messages added for in-project collaboration.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ agents
class AgentBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    kind: str = Field(default="agent", max_length=30)
    endpoint_id: int | None = None
    status: str = Field(default="idle", max_length=30)
    current_project_id: int | None = None
    last_conversation_snippet: str | None = None
    metadata_json: str | None = None


class AgentCreate(AgentBase):
    pass


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    kind: str | None = Field(default=None, max_length=30)
    endpoint_id: int | None = None
    status: str | None = Field(default=None, max_length=30)
    current_project_id: int | None = None
    last_conversation_snippet: str | None = None
    metadata_json: str | None = None


class AgentRead(AgentBase, ORMModel):
    id: int
    last_heartbeat: datetime | None = None
    created_at: datetime
    updated_at: datetime


class HeartbeatPayload(BaseModel):
    status: str | None = Field(default=None, max_length=30)
    conversation_snippet: str | None = None
    project_id: int | None = None


# ------------------------------------------------------------------ projects
class ProjectBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    status: str = Field(default="queued", max_length=30)
    agent_id: int | None = None
    repo_url: str | None = Field(default=None, max_length=500)
    branch: str | None = Field(default=None, max_length=200)
    conversation_id: str | None = Field(default=None, max_length=200)
    needs_approval: bool = False
    approval_note: str | None = None


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: str | None = Field(default=None, max_length=30)
    agent_id: int | None = None
    repo_url: str | None = Field(default=None, max_length=500)
    branch: str | None = Field(default=None, max_length=200)
    conversation_id: str | None = Field(default=None, max_length=200)
    needs_approval: bool | None = None
    approval_note: str | None = None


class ProjectRead(ProjectBase, ORMModel):
    id: int
    agent_name: str | None = None
    endpoint_name: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectApprovalRequest(BaseModel):
    note: str | None = None


class ProjectAssignRequest(BaseModel):
    agent_id: int | None = None


# ------------------------------------------------------------------ notifications
class NotificationRead(ORMModel):
    id: int
    level: str = "info"
    title: str
    body: str | None = None
    project_id: int | None = None
    agent_id: int | None = None
    read: bool = False
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ tasks
class TaskBase(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    status: str = Field(default="todo", max_length=30)
    agent_id: int | None = None
    position: int = 0


class TaskCreate(TaskBase):
    result_summary: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    status: str | None = Field(default=None, max_length=30)
    agent_id: int | None = None
    position: int | None = None
    result_summary: str | None = None


class TaskRead(TaskBase, ORMModel):
    id: int
    project_id: int
    agent_name: str | None = None
    result_summary: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskClaimRequest(BaseModel):
    agent_id: int = Field(ge=1, description="Agent claiming this task")
    status: str = Field(default="in_progress", max_length=30)


class TaskCompleteRequest(BaseModel):
    result_summary: str | None = Field(
        default=None,
        max_length=2000,
        description="Evidence: diff, PR link, test result, etc.",
    )


# ------------------------------------------------------------------ conversation messages
class ConversationMessageCreate(BaseModel):
    body: str = Field(min_length=1)
    agent_id: int | None = None


class ConversationMessageRead(ORMModel):
    id: int
    project_id: int
    agent_id: int | None = None
    agent_name: str | None = None
    body: str
    created_at: datetime


# ------------------------------------------------------------------ overview / activity
class SwarmOverview(BaseModel):
    agents_total: int = 0
    agents_working: int = 0
    projects_active: int = 0
    projects_needing_approval: int = 0
    notifications_unread: int = 0
    agents: list[AgentRead] = Field(default_factory=list)
    projects_needing_approval_list: list[ProjectRead] = Field(default_factory=list)


class ProjectActivity(BaseModel):
    project: ProjectRead
    notifications: list[NotificationRead] = Field(default_factory=list)
    messages: list[ConversationMessageRead] = Field(default_factory=list)
    tasks: list[TaskRead] = Field(default_factory=list)
    activity: list[dict[str, str | None]] = Field(default_factory=list)
