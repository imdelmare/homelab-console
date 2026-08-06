from typing import Literal

from pydantic import BaseModel, ConfigDict

ActorKind = Literal["user", "service", "agent", "telegram", "system"]


class Actor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ActorKind
    id: str
    label: str = ""

    def audit_id(self) -> str:
        return f"{self.kind}:{self.id}"
