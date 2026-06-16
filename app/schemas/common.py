from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class OrmSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


Importance = Annotated[int, Field(ge=1, le=10)]
Confidence = Annotated[float, Field(ge=0, le=1)]
