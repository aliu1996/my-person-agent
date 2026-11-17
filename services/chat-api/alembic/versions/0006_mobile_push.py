"""Store encrypted device registrations and their push queue."""

from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE push_device (
            token_hash CHAR(64) PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES sys_user ON DELETE CASCADE,
            token_ciphertext TEXT NOT NULL,
            platform TEXT NOT NULL CHECK (platform IN ('android', 'ios')),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_push_device_user ON push_device (user_id)")
    op.execute("""
        CREATE TABLE push_delivery (
            notification_id BIGINT NOT NULL REFERENCES user_notification ON DELETE CASCADE,
            token_hash CHAR(64) NOT NULL REFERENCES push_device ON DELETE CASCADE,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            delivered_at TIMESTAMPTZ,
            PRIMARY KEY (notification_id, token_hash)
        )
    """)
    op.execute("""
        CREATE INDEX idx_push_due ON push_delivery (next_attempt_at, notification_id)
        WHERE delivered_at IS NULL AND attempts < 5
    """)


def downgrade() -> None:
    op.execute("DROP TABLE push_delivery")
    op.execute("DROP TABLE push_device")
