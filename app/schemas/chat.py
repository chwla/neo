from datetime import datetime

from app.models.enums import ProjectStatus
from app.schemas.common import OrmSchema


class ProjectRead(OrmSchema):
    id: int
    name: str
    description: str | None
    status: ProjectStatus
    priority: int
    created_at: datetime
    updated_at: datetime
