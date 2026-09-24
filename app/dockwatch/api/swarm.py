"""REST API for the agent-swarm dashboard (agents / projects / notifications)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.api.deps import require_write
from app.dockwatch.api.inventory import conflict_handler, get_or_404
from app.dockwatch.database import get_session
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.models.swarm import Agent, Project, SwarmNotification
from app.dockwatch.models.swarm_tasks import ConversationMessage, Task
from app.dockwatch.schemas.swarm import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    ConversationMessageCreate,
    ConversationMessageRead,
    HeartbeatPayload,
    NotificationRead,
    ProjectActivity,
    ProjectApprovalRequest,
    ProjectAssignRequest,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    SwarmOverview,
    TaskClaimRequest,
    TaskCompleteRequest,
    TaskCreate,
    TaskRead,
    TaskUpdate,
)
from app.dockwatch.services.activity import activity_log

DB = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/api/swarm", tags=["swarm"])


# ------------------------------------------------------------------ helpers
async def _agent_names(db: AsyncSession, agent_ids: list[int]) -> dict[int, str]:
    if not agent_ids:
        return {}
    rows = (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()
    return {int(row[0]): str(row[1]) for row in rows}


async def _endpoint_names(db: AsyncSession, endpoint_ids: list[int]) -> dict[int, str]:
    if not endpoint_ids:
        return {}
    rows = (
        await db.execute(select(Endpoint.id, Endpoint.name).where(Endpoint.id.in_(endpoint_ids)))
    ).all()
    return {int(row[0]): str(row[1]) for row in rows}


async def _agent_endpoint_ids(db: AsyncSession, agent_ids: list[int]) -> dict[int, int]:
    """``{agent_id: endpoint_id}`` for agents that have one set."""
    if not agent_ids:
        return {}
    rows = (
        await db.execute(
            select(Agent.id, Agent.endpoint_id).where(
                Agent.id.in_(agent_ids), Agent.endpoint_id.is_not(None)
            )
        )
    ).all()
    return {int(row[0]): int(row[1]) for row in rows}


async def _project_read(db: AsyncSession, project: Project) -> ProjectRead:
    agent_name: str | None = None
    endpoint_name: str | None = None
    if project.agent_id is not None:
        names = await _agent_names(db, [project.agent_id])
        agent_name = names.get(project.agent_id)
        endpoint_ids = await _agent_endpoint_ids(db, [project.agent_id])
        endpoint_id = endpoint_ids.get(project.agent_id)
        if endpoint_id is not None:
            endpoint_names = await _endpoint_names(db, [endpoint_id])
            endpoint_name = endpoint_names.get(endpoint_id)
    return ProjectRead.model_validate(project).model_copy(
        update={"agent_name": agent_name, "endpoint_name": endpoint_name}
    )


async def _projects_read_bulk(db: AsyncSession, projects: list[Project]) -> list[ProjectRead]:
    agent_ids = sorted({p.agent_id for p in projects if p.agent_id is not None})
    agent_names = await _agent_names(db, agent_ids)
    agent_endpoint_ids = await _agent_endpoint_ids(db, agent_ids)
    endpoint_names = await _endpoint_names(db, sorted(set(agent_endpoint_ids.values())))
    out: list[ProjectRead] = []
    for project in projects:
        agent_name: str | None = None
        endpoint_name: str | None = None
        if project.agent_id is not None:
            agent_name = agent_names.get(project.agent_id)
            endpoint_id = agent_endpoint_ids.get(project.agent_id)
            if endpoint_id is not None:
                endpoint_name = endpoint_names.get(endpoint_id)
        out.append(
            ProjectRead.model_validate(project).model_copy(
                update={"agent_name": agent_name, "endpoint_name": endpoint_name}
            )
        )
    return out


# ------------------------------------------------------------------ agents
@router.get("", response_model=list[AgentRead])
async def list_agents(
    db: DB,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Agent]:
    rows = await db.scalars(select(Agent).order_by(Agent.name).limit(limit).offset(offset))
    return list(rows.all())


@router.post(
    "",
    response_model=AgentRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_agent(payload: AgentCreate, db: DB) -> Agent:
    agent = Agent(**payload.model_dump())
    db.add(agent)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(agent)
    activity_log.record("agent", "created", agent.name)
    return agent


@router.get("/agents/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: int, db: DB) -> Agent:
    agent: Agent = await get_or_404(db, Agent, agent_id, "Agent")
    return agent


@router.put(
    "/agents/{agent_id}",
    response_model=AgentRead,
    dependencies=[Depends(require_write)],
)
async def update_agent(agent_id: int, payload: AgentUpdate, db: DB) -> Agent:
    agent: Agent = await get_or_404(db, Agent, agent_id, "Agent")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(agent, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(agent)
    activity_log.record("agent", "updated", agent.name)
    return agent


@router.delete(
    "/agents/{agent_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_agent(agent_id: int, db: DB) -> None:
    agent: Agent = await get_or_404(db, Agent, agent_id, "Agent")
    await db.delete(agent)
    await db.commit()
    activity_log.record("agent", "deleted", agent.name)


@router.post(
    "/agents/{agent_id}/heartbeat",
    response_model=AgentRead,
    dependencies=[Depends(require_write)],
)
async def agent_heartbeat(agent_id: int, payload: HeartbeatPayload, db: DB) -> Agent:
    agent: Agent = await get_or_404(db, Agent, agent_id, "Agent")
    agent.touch_heartbeat(payload.status)
    if payload.conversation_snippet is not None:
        agent.last_conversation_snippet = payload.conversation_snippet
    if payload.project_id is not None:
        agent.current_project_id = payload.project_id
    await db.commit()
    await db.refresh(agent)
    activity_log.record("agent", "heartbeat", agent.name, detail=agent.status)
    return agent


# ------------------------------------------------------------------ projects
@router.get("/projects", response_model=list[ProjectRead])
async def list_projects(
    db: DB,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ProjectRead]:
    rows = await db.scalars(select(Project).order_by(Project.name).limit(limit).offset(offset))
    return await _projects_read_bulk(db, list(rows.all()))


@router.post(
    "/projects",
    response_model=ProjectRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_project(payload: ProjectCreate, db: DB) -> ProjectRead:
    project = Project(**payload.model_dump())
    db.add(project)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(project)
    activity_log.record("project", "created", project.name)
    return await _project_read(db, project)


@router.get("/projects/{project_id}", response_model=ProjectRead)
async def get_project(project_id: int, db: DB) -> ProjectRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    return await _project_read(db, project)


@router.put(
    "/projects/{project_id}",
    response_model=ProjectRead,
    dependencies=[Depends(require_write)],
)
async def update_project(project_id: int, payload: ProjectUpdate, db: DB) -> ProjectRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(project)
    activity_log.record("project", "updated", project.name)
    return await _project_read(db, project)


@router.delete(
    "/projects/{project_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_project(project_id: int, db: DB) -> None:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    await db.delete(project)
    await db.commit()
    activity_log.record("project", "deleted", project.name)


@router.post(
    "/projects/{project_id}/request-approval",
    response_model=ProjectRead,
    dependencies=[Depends(require_write)],
)
async def request_approval(project_id: int, payload: ProjectApprovalRequest, db: DB) -> ProjectRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    project.status = "needs_approval"
    project.needs_approval = True
    project.approval_note = payload.note
    db.add(
        SwarmNotification(
            level="approval",
            title=f"Approval requested: {project.name}",
            body=payload.note,
            project_id=project.id,
            agent_id=project.agent_id,
        )
    )
    if project.agent_id is not None:
        agent: Agent | None = await db.get(Agent, project.agent_id)
        if agent is not None:
            agent.status = "needs_approval"
    await db.commit()
    await db.refresh(project)
    activity_log.record("project", "request-approval", project.name, detail=payload.note)
    return await _project_read(db, project)


@router.post(
    "/projects/{project_id}/approve",
    response_model=ProjectRead,
    dependencies=[Depends(require_write)],
)
async def approve_project(
    project_id: int, db: DB, done: bool = Query(default=False)
) -> ProjectRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    project.status = "done" if done else "active"
    project.needs_approval = False
    pending = await db.scalars(
        select(SwarmNotification).where(
            SwarmNotification.project_id == project.id,
            SwarmNotification.level == "approval",
            SwarmNotification.read.is_(False),
        )
    )
    for notification in pending.all():
        notification.read = True
    db.add(
        SwarmNotification(
            level="info",
            title=f"Approved: {project.name}",
            body=f"Status set to {project.status}.",
            project_id=project.id,
            agent_id=project.agent_id,
        )
    )
    await db.commit()
    await db.refresh(project)
    activity_log.record("project", "approved", project.name, detail=project.status)
    return await _project_read(db, project)


@router.post(
    "/projects/{project_id}/assign",
    response_model=ProjectRead,
    dependencies=[Depends(require_write)],
)
async def assign_project(project_id: int, payload: ProjectAssignRequest, db: DB) -> ProjectRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    if payload.agent_id is not None:
        agent: Agent = await get_or_404(db, Agent, payload.agent_id, "Agent")
        project.agent_id = agent.id
    else:
        project.agent_id = None
    await db.commit()
    await db.refresh(project)
    activity_log.record("project", "assigned", project.name, detail=str(project.agent_id))
    return await _project_read(db, project)


@router.get("/projects/{project_id}/activity", response_model=ProjectActivity)
async def project_activity(project_id: int, db: DB) -> dict[str, Any]:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    rows = await db.scalars(
        select(SwarmNotification)
        .where(SwarmNotification.project_id == project.id)
        .order_by(SwarmNotification.id.desc())
        .limit(50)
    )
    notifications = [NotificationRead.model_validate(n) for n in rows.all()]
    task_rows = await db.scalars(
        select(Task).where(Task.project_id == project.id).order_by(Task.position)
    )
    tasks = [TaskRead.model_validate(t) for t in task_rows.all()]
    message_rows = await db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.project_id == project.id)
        .order_by(ConversationMessage.created_at.desc())
        .limit(30)
    )
    messages = [
        ConversationMessageRead.model_validate(m).model_dump(exclude_unset=True)
        for m in message_rows.all()
    ]
    return {
        "project": await _project_read(db, project),
        "notifications": notifications,
        "activity": activity_log.as_list()[:20],
        "tasks": tasks,
        "messages": messages,
    }


@router.get("/projects/{project_id}/tasks", response_model=list[TaskRead])
async def list_tasks(project_id: int, db: DB) -> list[TaskRead]:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    rows = await db.scalars(
        select(Task).where(Task.project_id == project.id).order_by(Task.position)
    )
    tasks = list(rows.all())
    if not tasks:
        return []
    agent_ids = sorted({t.agent_id for t in tasks if t.agent_id is not None})
    agent_names = await _agent_names(db, agent_ids)
    return [
        TaskRead.model_validate(t).model_copy(
            update={"agent_name": agent_names.get(t.agent_id) if t.agent_id is not None else None}
        )
        for t in tasks
    ]


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_task(project_id: int, payload: TaskCreate, db: DB) -> TaskRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    max_pos = (
        await db.scalar(select(func.max(Task.position)).where(Task.project_id == project.id)) or 0
    )
    next_pos = max_pos + 1
    task = Task(
        project_id=project.id,
        title=payload.title,
        description=payload.description,
        status=payload.status,
        agent_id=payload.agent_id,
        position=payload.position if payload.position else next_pos,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    activity_log.record("task", "created", task.title, detail=f"project={project.id}")
    return TaskRead.model_validate(task)


@router.get("/projects/{project_id}/tasks/{task_id}", response_model=TaskRead)
async def get_task(project_id: int, task_id: int, db: DB) -> TaskRead:
    task: Task = await get_or_404(db, Task, task_id, "Task")
    if task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Task not found")
    result = TaskRead.model_validate(task)
    if task.agent_id is not None:
        agent_names = await _agent_names(db, [task.agent_id])
        result = result.model_copy(update={"agent_name": agent_names.get(task.agent_id)})
    return result


@router.put(
    "/projects/{project_id}/tasks/{task_id}",
    response_model=TaskRead,
    dependencies=[Depends(require_write)],
)
async def update_task(project_id: int, task_id: int, payload: TaskUpdate, db: DB) -> TaskRead:
    task: Task = await get_or_404(db, Task, task_id, "Task")
    if task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Task not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, key, value)
    await db.commit()
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.delete(
    "/projects/{project_id}/tasks/{task_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_task(project_id: int, task_id: int, db: DB) -> None:
    task: Task = await get_or_404(db, Task, task_id, "Task")
    if task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Task not found")
    project_id_int = int(task.project_id)
    title = task.title
    await db.delete(task)
    await db.commit()
    activity_log.record("task", "deleted", title, detail=f"project={project_id_int}")


@router.post(
    "/projects/{project_id}/tasks/{task_id}/claim",
    response_model=TaskRead,
    status_code=200,
    dependencies=[Depends(require_write)],
)
async def claim_task(project_id: int, task_id: int, payload: TaskClaimRequest, db: DB) -> TaskRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    task: Task = await get_or_404(db, Task, task_id, "Task")
    if task.project_id != project.id:
        raise HTTPException(status_code=404, detail="Task not found")
    agent: Agent = await get_or_404(db, Agent, payload.agent_id, "Agent")
    if payload.status:
        task.status = payload.status
    else:
        task.status = "in_progress"
    task.agent_id = agent.id
    await db.commit()
    await db.refresh(task)
    names = await _agent_names(db, [agent.id])
    result = TaskRead.model_validate(task).model_copy(update={"agent_name": names.get(agent.id)})
    return result


@router.post(
    "/projects/{project_id}/tasks/{task_id}/complete",
    response_model=TaskRead,
    status_code=200,
    dependencies=[Depends(require_write)],
)
async def complete_task(
    project_id: int,
    task_id: int,
    payload: TaskCompleteRequest,
    db: DB,
) -> TaskRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    task: Task = await get_or_404(db, Task, task_id, "Task")
    if task.project_id != project.id:
        raise HTTPException(status_code=404, detail="Task not found")
    task.status = "done"
    task.agent_id = None
    if payload.result_summary is not None:
        task.result_summary = payload.result_summary
    await db.commit()
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.get("/tasks/claimable", response_model=list[TaskRead])
async def list_claimable_tasks(
    db: DB,
    project_id: int | None = Query(default=None, ge=1),
    status: str = Query(default="todo", pattern="^(todo|in_progress|blocked)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[TaskRead]:
    """Tasks an agent can pull for itself, optionally scoped to one project."""
    stmt = select(Task).where(Task.status == status, Task.agent_id.is_(None))
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    rows = await db.scalars(stmt.order_by(Task.position).limit(limit).offset(offset))
    tasks = list(rows.all())
    if not tasks:
        return []
    return [
        TaskRead.model_validate(t).model_copy(
            update={"agent_name": None, "result_summary": t.result_summary}
        )
        for t in tasks
    ]


# ------------------------------------------------------------------ conversation messages
@router.get(
    "/projects/{project_id}/messages",
    response_model=list[ConversationMessageRead],
)
async def list_messages(
    project_id: int,
    db: DB,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ConversationMessageRead]:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    rows = await db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.project_id == project.id)
        .order_by(ConversationMessage.id.desc())
        .limit(limit)
        .offset(offset)
    )
    messages = list(rows.all())
    if not messages:
        return []
    agent_ids = sorted({m.agent_id for m in messages if m.agent_id is not None})
    agent_names = await _agent_names(db, agent_ids)
    return [
        ConversationMessageRead.model_validate(m).model_copy(
            update={"agent_name": agent_names.get(m.agent_id) if m.agent_id is not None else None}
        )
        for m in messages
    ]


@router.post(
    "/projects/{project_id}/messages",
    response_model=ConversationMessageRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def post_message(
    project_id: int, payload: ConversationMessageCreate, db: DB
) -> ConversationMessageRead:
    project: Project = await get_or_404(db, Project, project_id, "Project")
    message = ConversationMessage(project_id=project.id, **payload.model_dump())
    db.add(message)
    await db.commit()
    await db.refresh(message)
    result = ConversationMessageRead.model_validate(message)
    if message.agent_id is not None:
        agent_names = await _agent_names(db, [message.agent_id])
        result = result.model_copy(update={"agent_name": agent_names.get(message.agent_id)})
    return result


# ------------------------------------------------------------------ tasks
@router.get("/notifications", response_model=list[NotificationRead])
async def list_notifications(
    db: DB,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[SwarmNotification]:
    stmt = select(SwarmNotification).order_by(SwarmNotification.id.desc())
    if unread_only:
        stmt = stmt.where(SwarmNotification.read.is_(False))
    rows = await db.scalars(stmt.limit(limit).offset(offset))
    return list(rows.all())


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationRead,
    dependencies=[Depends(require_write)],
)
async def mark_notification_read(notification_id: int, db: DB) -> SwarmNotification:
    notification: SwarmNotification = await get_or_404(
        db, SwarmNotification, notification_id, "Notification"
    )
    notification.read = True
    await db.commit()
    await db.refresh(notification)
    return notification


# ------------------------------------------------------------------ overview
@router.get("/overview", response_model=SwarmOverview)
async def swarm_overview(db: DB) -> dict[str, Any]:
    agents_total: int = await db.scalar(select(func.count(Agent.id))) or 0
    working_stmt = select(func.count(Agent.id)).where(Agent.status == "working")
    agents_working: int = await db.scalar(working_stmt) or 0
    active_stmt = select(func.count(Project.id)).where(Project.status == "active")
    projects_active: int = await db.scalar(active_stmt) or 0
    approval_stmt = select(func.count(Project.id)).where(Project.needs_approval.is_(True))
    projects_needing_approval: int = await db.scalar(approval_stmt) or 0
    unread_stmt = select(func.count(SwarmNotification.id)).where(SwarmNotification.read.is_(False))
    notifications_unread: int = await db.scalar(unread_stmt) or 0

    agent_rows = await db.scalars(select(Agent).order_by(Agent.name))
    agents = [AgentRead.model_validate(a) for a in agent_rows.all()]
    approval_rows = await db.scalars(
        select(Project).where(Project.needs_approval.is_(True)).order_by(Project.name)
    )
    approval_projects = await _projects_read_bulk(db, list(approval_rows.all()))
    return {
        "agents_total": agents_total,
        "agents_working": agents_working,
        "projects_active": projects_active,
        "projects_needing_approval": projects_needing_approval,
        "notifications_unread": notifications_unread,
        "agents": agents,
        "projects_needing_approval_list": approval_projects,
    }
