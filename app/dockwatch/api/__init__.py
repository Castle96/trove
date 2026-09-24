"""API routers."""

from app.dockwatch.api.container_ranking import router as container_ranking
from app.dockwatch.api.docker import router as docker_router
from app.dockwatch.api.endpoints import router as endpoints_router
from app.dockwatch.api.inventory import router as inventory_router
from app.dockwatch.api.metrics import router as metrics_router
from app.dockwatch.api.models import router as models_router
from app.dockwatch.api.monitor import router as monitor_router
from app.dockwatch.api.security import router as security_router
from app.dockwatch.api.swarm import router as swarm_router
from app.dockwatch.api.voice import router as voice_router

__all__ = [
    "container_ranking",
    "docker_router",
    "endpoints_router",
    "inventory_router",
    "metrics_router",
    "models_router",
    "monitor_router",
    "security_router",
    "swarm_router",
    "voice_router",
]
