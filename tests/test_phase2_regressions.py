import os
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from chat_api.client.auth import issue_token, otp_keys, private_hash
from chat_api.client.sessions import read_branch
from chat_api.database import database
from chat_api.main import app
from pa_shared.db import create_engine
from pa_shared.points import refresh_point_account, settle_turn_points
from pa_shared.storage import presign
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class Account:
    def __init__(self):
        self.version = 0
        self.active = True

    async def execute(self, query, params):
        assert "status = 'active'" in str(query) and "token_version" in str(query)
        self.result = {"id": 11} if self.active and params["version"] == self.version else None
        return self

    def mappings(self):
        return self

    def first(self):
        return self.result

    async def scalar(self, query, params):
        assert "token_version = token_version + 1" in str(query) and params == {"id": 11}
        self.active = False
        self.version += 1
        return datetime.now(UTC)

    async def commit(self):
        pass


async def test_account_deletion_invalidates_access_and_refresh_after_recovery():
    account = Account()

    async def fake_database():
        yield account

    app.dependency_overrides[database] = fake_database
    access = issue_token(11, 0, "user", 7)
    refresh = issue_token(11, 0, "user_refresh", 30)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://app"
        ) as client:
            wrong_role = await client.post(
                "/api/user/deleteAccount", headers={"Authorization": f"Bearer {refresh}"}
            )
            assert wrong_role.json()["code"] == 4010

            deleted = await client.post(
                "/api/user/deleteAccount", headers={"Authorization": f"Bearer {access}"}
            )
            assert deleted.json()["code"] == 0

            account.active = True
            rejected_access = await client.get(
                "/api/user/profile", headers={"Authorization": f"Bearer {access}"}
            )
            rejected_refresh = await client.post(
                "/api/auth/refresh", json={"refreshToken": refresh}
            )
            assert rejected_access.json()["code"] == rejected_refresh.json()["code"] == 4010
    finally:
        del app.dependency_overrides[database]


def test_otp_redis_keys_hide_phone_and_share_cluster_slot():
    phone = "13800138000"
    identifier = private_hash("phone", phone)
    keys = otp_keys(identifier)
    assert all(phone not in key and ("{" + identifier + "}") in key for key in keys)
    assert identifier != private_hash("share-report", phone)


def test_presign_binds_upload_length_type_and_no_overwrite():
    url = presign(
        "put_object",
        "uploads/1/demo.txt",
        600,
        ContentType="text/plain",
        ContentLength=4,
        IfNoneMatch="*",
    )
    params = parse_qs(urlsplit(url).query)
    assert params["X-Amz-Expires"] == ["600"]
    assert set(params["X-Amz-SignedHeaders"][0].split(";")) >= {
        "content-length",
        "content-type",
        "host",
        "if-none-match",
    }


@pytest.mark.skipif(
    not os.getenv("PHASE2_TEST_DATABASE_URL"),
    reason="Configure a migrated PostgreSQL test database",
)
async def test_postgres_points_and_message_branch():
    engine = create_engine(os.environ["PHASE2_TEST_DATABASE_URL"])
    try:
        async with AsyncSession(engine) as db:
            user_id = await db.scalar(
                text("""
                INSERT INTO sys_user (phone_ciphertext, phone_hash)
                VALUES ('test-cipher', :hash) RETURNING id
            """),
                {"hash": uuid4().hex},
            )
            amount = await db.scalar(
                text("""
                SELECT rule_value FROM point_rule WHERE rule_key = 'daily_free_amount'
            """)
            )
            assert await refresh_point_account(db, user_id) == amount
            await db.execute(
                text("""
                UPDATE point_bucket SET expire_at = now() - interval '1 day', balance = 0
                WHERE user_id = :user AND bucket_type = 'daily_free'
            """),
                {"user": user_id},
            )
            assert await refresh_point_account(db, user_id) == amount
            turn = uuid4()
            first = await settle_turn_points(db, user_id, turn, amount + 45, {})
            replay = await settle_turn_points(db, user_id, turn, 1, {})
            assert first.charged == replay.charged == amount + 20
            assert first.balance_after == replay.balance_after == -20 and replay.replayed
            assert await refresh_point_account(db, user_id) == -20

            session = await db.scalar(
                text("""
                INSERT INTO chat_session (user_id, title) VALUES (:user, 'branch') RETURNING id
            """),
                {"user": user_id},
            )
            insert = text("""
                INSERT INTO chat_message (session_id, parent_id, turn_id, role, status, content)
                VALUES (:session, :parent, :turn, :role, 'done', :content)
                RETURNING id
            """)
            root = await db.scalar(
                insert,
                {
                    "session": session,
                    "parent": None,
                    "turn": turn,
                    "role": "user",
                    "content": "root",
                },
            )
            first_leaf = await db.scalar(
                insert,
                {
                    "session": session,
                    "parent": root,
                    "turn": turn,
                    "role": "assistant",
                    "content": "first",
                },
            )
            sibling = await db.scalar(
                insert,
                {
                    "session": session,
                    "parent": root,
                    "turn": turn,
                    "role": "assistant",
                    "content": "second",
                },
            )
            branch = await read_branch(db, session, sibling)
            assert [message["content"] for message in branch] == ["root", "second"]
            assert branch[1]["siblingIds"] == [first_leaf]
            assert [m["role"] for m in await read_branch(db, session, sibling, public=True)] == [
                "user",
                "assistant",
            ]
            await db.rollback()
    finally:
        await engine.dispose()
