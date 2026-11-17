from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


class ScheduleSpec(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: Annotated[str, Field(min_length=1, max_length=160)]
    prompt: Annotated[str, Field(min_length=1, max_length=24000)]
    kind: Literal["once", "daily"]
    runAt: AwareDatetime | None = None
    dailyAt: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")] | None = None
    webSearch: bool = False

    @model_validator(mode="after")
    def validate_time(self):
        if self.kind == "once" and self.runAt and not self.dailyAt:
            return self
        if self.kind == "daily" and self.dailyAt and self.runAt is None:
            return self
        raise ValueError("Provide runAt for one-time reminders or dailyAt for daily schedules")

    @property
    def cron(self) -> str | None:
        if self.dailyAt is None:
            return None
        hour, minute = map(int, self.dailyAt.split(":"))
        return f"{minute} {hour} * * *"

    def next_run(self, now: datetime) -> datetime | None:
        if self.runAt:
            return self.runAt.astimezone(UTC) if self.runAt > now else None
        return next_daily(self.cron, now)


def next_daily(cron: str | None, now: datetime) -> datetime | None:
    if cron is None:
        return None
    parts = cron.split()
    if len(parts) != 5 or parts[2:] != ["*"] * 3:
        return None
    minute, hour = map(int, parts[:2])
    local_now = now.astimezone(SHANGHAI)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
