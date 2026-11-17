from datetime import UTC, datetime

import pytest
from chat_api.client.common import ClientError
from chat_api.client.library import asset_identifier
from pa_shared.schedule import ScheduleSpec, next_daily


def test_daily_schedule_uses_local_day_and_skips_already_due_occurrence():
    now = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    spec = ScheduleSpec(title="Daily", prompt="Get headlines", kind="daily", dailyAt="08:00")
    assert spec.cron == "0 8 * * *"
    assert next_daily(spec.cron, now) == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)


def test_schedule_has_an_explicit_offset_and_one_time_requests_must_be_future():
    now = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    spec = ScheduleSpec(
        title="Reminder", prompt="Check inbox", kind="once", runAt="2026-11-17T08:01:00+08:00"
    )
    assert spec.next_run(now) == datetime(2026, 11, 17, 0, 1, tzinfo=UTC)
    assert spec.next_run(datetime(2026, 11, 17, 0, 2, tzinfo=UTC)) is None


def test_library_distinguishes_upload_and_artifact_ids():
    assert asset_identifier("file-42") == ("file", 42)
    assert asset_identifier("artifact-42") == ("artifact", 42)
    for rejected in ("file-0", "../artifact-1", "file-9223372036854775808", "file-１２"):
        with pytest.raises(ClientError):
            asset_identifier(rejected)
