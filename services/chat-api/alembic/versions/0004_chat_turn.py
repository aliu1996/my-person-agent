"""Store turn admission, idempotent settlement and pending memory extraction."""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.execute("ALTER TABLE chat_message ALTER COLUMN point_cost TYPE BIGINT")
    op.execute("""
        CREATE TABLE chat_turn (
            id UUID PRIMARY KEY,
            user_id BIGINT NOT NULL,
            session_id BIGINT NOT NULL,
            token_version BIGINT NOT NULL,
            base_leaf_id BIGINT,
            parent_id BIGINT,
            message_id BIGINT NOT NULL,
            content TEXT,
            model_code TEXT NOT NULL,
            costs JSONB NOT NULL,
            is_temporary BOOLEAN NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            result JSONB,
            memory_pending BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (user_id, session_id) REFERENCES chat_session (user_id, id)
                ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX idx_turn_session ON chat_turn (user_id, session_id)")
    op.execute(
        "CREATE INDEX idx_turn_memory_pending ON chat_turn (created_at) WHERE memory_pending"
    )
    op.execute("CREATE INDEX idx_memory_turn ON user_memory (source_turn_id)")


def downgrade() -> None:
    op.execute("DROP INDEX idx_memory_turn")
    op.execute("DROP TABLE chat_turn")
    op.execute("ALTER TABLE chat_message ALTER COLUMN point_cost TYPE INTEGER")
