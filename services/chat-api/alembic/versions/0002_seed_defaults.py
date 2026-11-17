"""Seed operator and platform defaults."""

import sqlalchemy as sa
from alembic import context, op
from pa_shared import get_settings
from pa_shared.security import hash_password

revision = "0002"
down_revision = "0001"

admin = sa.table("admin_user", sa.column("username", sa.Text), sa.column("password_hash", sa.Text))


def upgrade() -> None:
    settings = get_settings()
    existing = (
        not context.is_offline_mode()
        and op.get_bind()
        .execute(
            sa.select(admin.c.username).where(admin.c.username == settings.init_admin_username)
        )
        .first()
    )
    if not existing:
        password = settings.init_admin_password
        if password is None or not password.get_secret_value():
            raise RuntimeError("Set INIT_ADMIN_PASSWORD before seeding the initial administrator.")
        op.execute(
            sa.text(
                "INSERT INTO admin_user (username, password_hash) VALUES (:username, :hash) "
                "ON CONFLICT (username) DO NOTHING"
            ).bindparams(
                sa.bindparam("username", settings.init_admin_username, type_=sa.Text()),
                sa.bindparam("hash", hash_password(password.get_secret_value()), type_=sa.Text()),
            )
        )
    op.execute(
        "INSERT INTO ai_bot (name, is_builtin, welcome_msg) "
        "VALUES ('全能助手', TRUE, '你好，有什么可以帮你？') ON CONFLICT DO NOTHING"
    )
    op.execute(
        "INSERT INTO point_rule (rule_key, rule_value) VALUES "
        "('daily_free_amount', 1000), ('search_cost', 2), ('sandbox_10s_cost', 1), "
        "('image_cost', 15) ON CONFLICT (rule_key) DO NOTHING"
    )


def downgrade() -> None:
    # The schema downgrade removes these tables. Keep operator data on a seed-only downgrade.
    pass
