from tests.conftest import do_login


async def test_switch_active_model(db_session, user):
    from app.domain.actors import Actor
    from app.services.model_providers import list_model_profiles, switch_active_model

    profiles = await list_model_profiles(db_session)
    assert {profile.id for profile in profiles} == {"claude", "codex"}

    actor = Actor(kind="user", id=str(user.id))
    await switch_active_model(db_session, "codex", actor, source="test")
    await db_session.commit()

    profiles = await list_model_profiles(db_session)
    active = [profile.id for profile in profiles if profile.active]
    assert active == ["codex"]


def test_invalid_provider_rejected():
    from app.services.model_providers import get_model_provider

    assert get_model_provider("gpt-neo") is None


async def test_task_creation_does_not_depend_on_active_model(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}

    created = await client.post(
        "/api/tasks", json={"title": "fix frigate", "goal": "camera offline"}, headers=headers
    )
    assert created.status_code == 201
    task = created.json()
    # assigned_agent (set only via claim) is the only field that determines
    # task ownership.
    assert task["assigned_agent"] == ""

    # The per-task model-switch endpoint no longer exists.
    removed = await client.post(
        f"/api/tasks/{task['id']}/model", json={"provider": "codex"}, headers=headers
    )
    assert removed.status_code == 404


async def test_handoff_context_contains_no_secrets(client, user, capture_adapter, db_session):
    from app.services.model_providers import build_handoff_context
    from app.services.tasks_service import add_check, add_finding, create_task
    from app.domain.actors import Actor

    actor = Actor(kind="user", id="operator")
    task = await create_task(db_session, "t", "g", actor)
    await add_check(db_session, task.id, "verify gateway", actor)
    await add_finding(db_session, task.id, "warning", "Latency high", "gateway slow", actor)
    await db_session.commit()

    context = await build_handoff_context(db_session, task)
    assert context.task_id == task.id
    assert context.pending_checks == ["verify gateway"]
    assert context.completed_checks == []
    assert context.findings[0]["title"] == "Latency high"
    # Handoff context is built from fixed-shape canonical state only: no
    # free-form dicts where a credential-bearing key could ride along.
    allowed = {"severity", "title", "description", "created_at", "resolved_at", "tool_invocation_id"}
    assert set(context.findings[0]) == allowed
