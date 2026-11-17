"""Allow independent Library retention and durable Agent and schedule receipts."""

from alembic import op

revision = "0005"
down_revision = "0004"


UPGRADE = """
        ALTER TABLE session_artifact ALTER COLUMN session_id DROP NOT NULL;
        ALTER TABLE session_artifact ADD COLUMN turn_id UUID;
        CREATE UNIQUE INDEX idx_artifact_turn_key ON session_artifact (turn_id, s3_key)
            WHERE turn_id IS NOT NULL;
        CREATE INDEX idx_artifact_library ON session_artifact (user_id, created_at DESC, id DESC)
            WHERE deleted_at IS NULL;
        CREATE INDEX idx_upload_library ON user_file (user_id, created_at DESC, id DESC)
            WHERE deleted_at IS NULL;
        ALTER TABLE chat_turn ADD COLUMN file_ids BIGINT[] NOT NULL DEFAULT '{}';
        ALTER TABLE chat_turn ADD COLUMN archive_key TEXT;
        ALTER TABLE chat_schedule_task ADD COLUMN confirmation_id UUID UNIQUE;
        ALTER TABLE chat_schedule_task ADD COLUMN web_search BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE chat_schedule_task ADD COLUMN run_once_at TIMESTAMPTZ;
        ALTER TABLE chat_schedule_log ADD COLUMN scheduled_at TIMESTAMPTZ;
        ALTER TABLE chat_schedule_log ADD COLUMN lease_until TIMESTAMPTZ;
        ALTER TABLE chat_schedule_log ALTER COLUMN point_cost TYPE BIGINT;
        CREATE UNIQUE INDEX idx_schedule_occurrence
            ON chat_schedule_log (task_id, scheduled_at) WHERE scheduled_at IS NOT NULL;
        CREATE INDEX idx_schedule_log_queue ON chat_schedule_log (id)
            WHERE status IN ('queued', 'running');
        CREATE INDEX idx_activity_user ON user_notification (user_id, created_at DESC, id DESC);
"""
DOWNGRADE = """
        DROP INDEX idx_activity_user;
        DROP INDEX idx_schedule_log_queue;
        DROP INDEX idx_schedule_occurrence;
        ALTER TABLE chat_schedule_log ALTER COLUMN point_cost TYPE INTEGER;
        ALTER TABLE chat_schedule_log DROP COLUMN lease_until, DROP COLUMN scheduled_at;
        ALTER TABLE chat_schedule_task DROP COLUMN run_once_at, DROP COLUMN web_search,
            DROP COLUMN confirmation_id;
        ALTER TABLE chat_turn DROP COLUMN archive_key, DROP COLUMN file_ids;
        DROP INDEX idx_upload_library;
        DROP INDEX idx_artifact_library;
        DROP INDEX idx_artifact_turn_key;
        ALTER TABLE session_artifact DROP COLUMN turn_id;
"""


def upgrade() -> None:
    for statement in UPGRADE.strip().rstrip(";").split(";"):
        op.execute(statement.strip())


def downgrade() -> None:
    for statement in DOWNGRADE.strip().rstrip(";").split(";"):
        op.execute(statement.strip())
    # Detached artifacts have no former session to reattach to on downgrade.
