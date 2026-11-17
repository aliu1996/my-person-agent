from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Id = Annotated[int, Field(gt=0, lt=2**63)]
ModelCode = Annotated[str, Field(min_length=1, max_length=100)]
ChatText = Annotated[str, Field(min_length=1, max_length=24000)]


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sessionId: Id
    parentMsgId: Id | None = None
    content: ChatText
    modelCode: ModelCode | None = None
    webSearch: bool = False
    fileIds: Annotated[list[Id], Field(max_length=20)] = Field(default_factory=list)
    skillIds: Annotated[list[Id], Field(max_length=20)] = Field(default_factory=list)
    isTemporary: bool = False


class PrepareTurn(TurnInput):
    turnId: UUID


class TurnUsage(BaseModel):
    inputTokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    cachedTokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    outputTokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    estimated: bool = False


class ModerationHit(BaseModel):
    direction: Literal["input", "output"]
    layer: Literal["dfa", "regex", "model"]
    snippet: Annotated[str, Field(max_length=240)]


class FinishTurn(BaseModel):
    status: Literal["done", "stopped", "failed", "blocked"]
    routeType: Literal["fast", "agent"] = "fast"
    output: Annotated[str, Field(max_length=96000)] = ""
    usage: TurnUsage = Field(default_factory=TurnUsage)
    searchCalls: Annotated[int, Field(ge=0, le=12)] = 0
    sandboxMs: Annotated[int, Field(ge=0, le=360_000)] = 0
    hit: ModerationHit | None = None
