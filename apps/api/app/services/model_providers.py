"""Model-provider abstraction. Claude and Codex are replaceable reasoning
engines: they consume shared task state and never own it, and they never
receive credentials.

This milestone ships mock adapters; switching, persistence, audit and the
structured handoff context are real.
"""

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Finding, ModelProfile, Task, TaskCheck, ToolInvocation, utcnow
from app.domain.actors import Actor
from app.services.audit import write_audit
from app.services.redaction import redact

MODEL_PROVIDER_IDS = ("claude", "codex")
ModelProviderId = Literal["claude", "codex"]


class ModelProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str  # mock | configured | error


class TaskContext(BaseModel):
    """Structured handoff context passed to a model provider. Built from
    canonical task state; secrets are redacted defensively even though none
    should ever be present."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    goal: str
    status: str
    summary: str
    pending_checks: list
    completed_checks: list
    findings: list[dict]
    recent_invocations: list[dict]
    available_tools: list[str]


class ModelRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    detail: str = ""


class ModelProvider:
    id: str = ""
    name: str = ""

    async def health(self) -> ModelProviderHealth:
        return ModelProviderHealth(id=self.id, status="mock")

    async def run_task(self, context: TaskContext) -> ModelRunResult:
        return ModelRunResult(ok=False, detail=f"{self.id} adapter is a mock in this milestone")


class ClaudeProvider(ModelProvider):
    id = "claude"
    name = "Claude"


class CodexProvider(ModelProvider):
    id = "codex"
    name = "Codex"


_MODEL_PROVIDERS: dict[str, ModelProvider] = {
    provider.id: provider for provider in (ClaudeProvider(), CodexProvider())
}


def get_model_provider(provider_id: str) -> ModelProvider | None:
    return _MODEL_PROVIDERS.get(provider_id)


async def seed_model_profiles(db: AsyncSession) -> None:
    for index, provider in enumerate(_MODEL_PROVIDERS.values()):
        row = await db.get(ModelProfile, provider.id)
        if row is None:
            db.add(ModelProfile(id=provider.id, name=provider.name, status="mock", active=index == 0))
    await db.flush()


async def list_model_profiles(db: AsyncSession) -> list[ModelProfile]:
    result = await db.execute(select(ModelProfile).order_by(ModelProfile.id))
    return list(result.scalars())


async def get_active_model(db: AsyncSession) -> ModelProfile | None:
    result = await db.execute(select(ModelProfile).where(ModelProfile.active.is_(True)))
    return result.scalars().first()


async def switch_active_model(db: AsyncSession, provider_id: str, actor: Actor, source: str) -> ModelProfile:
    """Operator-scoped switch only: which model provider the operator plane
    (Telegram/future web chat) considers "active" for conversation. This has
    no effect on task ownership — that is `assigned_agent`, set exclusively
    via `tasks_service.claim_task`."""
    if provider_id not in _MODEL_PROVIDERS:
        raise ValueError(f"unknown model provider: {provider_id}")

    profiles = await list_model_profiles(db)
    target = None
    for profile in profiles:
        profile.active = profile.id == provider_id
        profile.updated_at = utcnow()
        if profile.id == provider_id:
            target = profile

    await write_audit(
        db,
        actor=actor,
        source=source,
        action="model.switch",
        outcome="success",
        task_id="",
        metadata={"to": provider_id, "scope": "operator"},
    )
    await db.flush()
    return cast(ModelProfile, target)


async def build_handoff_context(db: AsyncSession, task: Task) -> TaskContext:
    """Structured handoff for the incoming model. Contains task facts and
    evidence references only — never credentials or raw transcripts."""
    findings_result = await db.execute(
        select(Finding).where(Finding.task_id == task.id).order_by(Finding.created_at)
    )
    checks_result = await db.execute(
        select(TaskCheck).where(TaskCheck.task_id == task.id).order_by(TaskCheck.created_at)
    )
    invocations_result = await db.execute(
        select(ToolInvocation)
        .where(ToolInvocation.task_id == task.id)
        .order_by(ToolInvocation.started_at.desc())
        .limit(20)
    )

    from app.tools.registry import list_tools

    findings = [
        {
            "severity": finding.severity,
            "title": finding.title,
            "description": finding.description,
            "created_at": finding.created_at.isoformat() if finding.created_at else None,
            "resolved_at": finding.resolved_at.isoformat() if finding.resolved_at else None,
            "tool_invocation_id": finding.tool_invocation_id,
        }
        for finding in findings_result.scalars()
    ]
    checks = list(checks_result.scalars())
    invocations = [
        {
            "invocation_id": invocation.id,
            "tool_id": invocation.tool_id,
            "status": invocation.status,
            "error_code": invocation.error_code,
            "started_at": invocation.started_at.isoformat() if invocation.started_at else None,
        }
        for invocation in invocations_result.scalars()
    ]

    return TaskContext(
        task_id=task.id,
        title=task.title,
        goal=task.goal,
        status=task.status,
        summary=task.summary,
        pending_checks=redact(
            [check.description for check in checks if check.status == "pending"]
        ),
        completed_checks=redact(
            [check.description for check in checks if check.status in ("completed", "skipped")]
        ),
        findings=redact(findings),
        recent_invocations=redact(invocations),
        available_tools=[tool.id for tool in list_tools() if tool.enabled and tool.mode == "read"],
    )
