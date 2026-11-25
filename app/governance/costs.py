"""按调研运行用量归集租户/工作区/模型成本。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResearchRun


@dataclass(frozen=True, slots=True)
class CostBucket:
    workspace_id: UUID
    project_id: UUID | None
    model_provider: str
    model_name: str
    runs: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    tool_calls: int


@dataclass(frozen=True, slots=True)
class CostSummary:
    since: datetime
    until: datetime
    runs: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    tool_calls: int
    buckets: list[CostBucket]


def _usage_int(field: str) -> object:
    raw = ResearchRun.usage.op("->>")(field)
    return func.coalesce(cast(func.nullif(raw, ""), Integer), 0)


async def cost_summary(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    since: datetime,
    until: datetime,
) -> CostSummary:
    input_tokens = _usage_int("input_tokens")
    output_tokens = _usage_int("output_tokens")
    cost_micros = _usage_int("cost_micros")
    tool_calls = _usage_int("tool_calls")
    statement = (
        select(
            ResearchRun.workspace_id,
            ResearchRun.project_id,
            ResearchRun.model_provider,
            ResearchRun.model_name,
            func.count(ResearchRun.id),
            func.coalesce(func.sum(input_tokens), 0),
            func.coalesce(func.sum(output_tokens), 0),
            func.coalesce(func.sum(cost_micros), 0),
            func.coalesce(func.sum(tool_calls), 0),
        )
        .where(
            ResearchRun.tenant_id == tenant_id,
            ResearchRun.created_at >= since,
            ResearchRun.created_at < until,
        )
        .group_by(
            ResearchRun.workspace_id,
            ResearchRun.project_id,
            ResearchRun.model_provider,
            ResearchRun.model_name,
        )
        .order_by(ResearchRun.workspace_id, ResearchRun.project_id, ResearchRun.model_name)
    )
    buckets: list[CostBucket] = []
    total_runs = 0
    total_input = 0
    total_output = 0
    total_cost = 0
    total_tools = 0
    for row in (await session.execute(statement)).all():
        bucket = CostBucket(
            workspace_id=row[0],
            project_id=row[1],
            model_provider=row[2],
            model_name=row[3],
            runs=int(row[4]),
            input_tokens=int(row[5]),
            output_tokens=int(row[6]),
            cost_micros=int(row[7]),
            tool_calls=int(row[8]),
        )
        buckets.append(bucket)
        total_runs += bucket.runs
        total_input += bucket.input_tokens
        total_output += bucket.output_tokens
        total_cost += bucket.cost_micros
        total_tools += bucket.tool_calls
    return CostSummary(
        since=since,
        until=until,
        runs=total_runs,
        input_tokens=total_input,
        output_tokens=total_output,
        cost_micros=total_cost,
        tool_calls=total_tools,
        buckets=buckets,
    )


__all__ = ["CostBucket", "CostSummary", "cost_summary"]
