import httpx
import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import TaskEvent
from app.domain.actors import Actor
from app.services.fixer_dispatch import assign_and_dispatch_fixer, dispatch_fixer
from app.services.tasks_service import (
    TaskServiceError,
    claim_task,
    create_task,
    record_fixer_dispatch_requested,
)


OPERATOR = Actor(kind="user", id="operator", label="operator")


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, json, headers):
        self.request = {"url": url, "json": json, "headers": headers}
        return self.response


async def test_assign_and_dispatch_fixer_uses_narrow_payload(db_session, monkeypatch):
    monkeypatch.setenv("FIXER_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("FIXER_DISPATCH_URL", "http://10.0.0.121:8767/fixer")
    monkeypatch.setenv("FIXER_DISPATCH_SECRET", "test-only-secret")
    get_settings.cache_clear()
    fake = _FakeClient(httpx.Response(202))
    monkeypatch.setattr(
        "app.services.fixer_dispatch.httpx.AsyncClient",
        lambda **_kwargs: fake,
    )

    task = await create_task(db_session, "Fix service", "Manual dispatch", OPERATOR)
    task, result = await assign_and_dispatch_fixer(db_session, task.id, OPERATOR, source="rest")

    assert task.assigned_agent == "agent:fixer"
    assert task.status == "claimed"
    assert result.ok is True
    assert result.code == "accepted"
    assert fake.request is not None
    assert fake.request["url"] == "http://10.0.0.121:8767/fixer"
    assert fake.request["json"] == {"task_id": task.id}
    assert fake.request["headers"] == {"X-Secret": "test-only-secret"}
    event = await db_session.scalar(
        select(TaskEvent).where(
            TaskEvent.task_id == task.id,
            TaskEvent.kind == "task.fixer_dispatch_requested",
        )
    )
    assert event is not None
    assert event.payload == {
        "assigned_agent": "agent:fixer",
        "authorized_by": "user:operator",
        "dispatch_kind": "operator",
    }


async def test_fixer_dispatch_does_not_steal_another_agents_task(db_session):
    task = await create_task(db_session, "Owned task", "Do not steal", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", OPERATOR)

    with pytest.raises(TaskServiceError) as exc:
        await assign_and_dispatch_fixer(db_session, task.id, OPERATOR, source="rest")

    assert exc.value.code == "task_already_claimed"
    assert task.assigned_agent == "agent:claude"


async def test_dispatch_fixer_requires_matching_authorization_event(db_session):
    task = await create_task(db_session, "Fix service", "Manual dispatch", OPERATOR)
    await claim_task(db_session, task.id, "agent:fixer", OPERATOR)
    other = await create_task(db_session, "Other service", "Other dispatch", OPERATOR)
    await claim_task(db_session, other.id, "agent:fixer", OPERATOR)
    authorization = await record_fixer_dispatch_requested(
        db_session,
        other,
        OPERATOR,
        source="rest",
    )

    with pytest.raises(TaskServiceError) as exc:
        await dispatch_fixer(
            db_session,
            task.id,
            OPERATOR,
            source="rest",
            authorization_event_id=authorization.id,
        )

    assert exc.value.code == "dispatch_not_authorized"


async def test_dispatch_fixer_rejects_authorization_from_another_actor(db_session):
    task = await create_task(db_session, "Fix service", "Manual dispatch", OPERATOR)
    await claim_task(db_session, task.id, "agent:fixer", OPERATOR)
    authorization = await record_fixer_dispatch_requested(
        db_session,
        task,
        OPERATOR,
        source="rest",
    )

    with pytest.raises(TaskServiceError) as exc:
        await dispatch_fixer(
            db_session,
            task.id,
            Actor(kind="user", id="other", label="other"),
            source="rest",
            authorization_event_id=authorization.id,
        )

    assert exc.value.code == "dispatch_not_authorized"
