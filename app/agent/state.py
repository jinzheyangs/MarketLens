"""Structured, JSON-serializable LangGraph state and budget helpers."""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict
from uuid import UUID

from app.agent.enums import SPECIALIST_ROLES, AgentRole
from app.core.config import Settings


class ResearchState(TypedDict, total=False):
    run_id: str
    objective: str
    workspace_id: str
    project_id: str | None
    round: int
    round_start_findings: int
    plan: list[dict[str, Any]]
    findings: Annotated[list[dict[str, Any]], operator.add]
    task_results: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
    usage: Annotated[list[dict[str, Any]], operator.add]
    phase: str
    stop_reason: str | None
    request_new_plan: bool
    request_retry: bool
    aggregate_needed: bool
    budget: dict[str, Any]


@dataclass(frozen=True, slots=True)
class UsageTotals:
    input_tokens: int
    output_tokens: int
    cost_micros: int
    tool_calls: int

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_micros": self.cost_micros,
            "tool_calls": self.tool_calls,
        }


def budget_for_settings(settings: Settings) -> dict[str, Any]:
    return {
        "max_rounds": settings.agent_max_rounds,
        "max_parallel_tasks": settings.agent_max_parallel_tasks,
        "max_task_attempts": settings.agent_max_task_attempts,
        "max_findings_per_task": settings.agent_max_findings_per_task,
        "max_findings": settings.agent_max_findings,
        "max_input_tokens": settings.agent_max_input_tokens,
        "max_output_tokens": settings.agent_max_output_tokens,
        "max_output_tokens_per_call": settings.agent_max_output_tokens_per_call,
        "max_cost_micros": settings.agent_max_cost_micros,
        "input_cost_micros_per_1k": settings.agent_input_cost_micros_per_1k,
        "output_cost_micros_per_1k": settings.agent_output_cost_micros_per_1k,
        "max_tool_calls": settings.agent_max_tool_calls,
        "max_tool_calls_per_task": settings.agent_max_tool_calls_per_task,
        "max_seconds": settings.agent_max_seconds,
        "task_timeout_seconds": settings.agent_task_timeout_seconds,
        "min_findings_for_followup": settings.agent_min_findings_for_followup,
        "agent_roles": [role.value for role in SPECIALIST_ROLES],
    }


def initial_state(
    *,
    run_id: UUID,
    objective: str,
    workspace_id: UUID,
    project_id: UUID | None,
    settings: Settings,
) -> dict[str, Any]:
    return {
        "run_id": str(run_id),
        "objective": objective,
        "workspace_id": str(workspace_id),
        "project_id": str(project_id) if project_id else None,
        "round": 0,
        "round_start_findings": 0,
        "plan": [],
        "findings": [],
        "task_results": [],
        "errors": [],
        "usage": [],
        "phase": "initialized",
        "stop_reason": None,
        "request_new_plan": False,
        "request_retry": False,
        "aggregate_needed": False,
        "budget": budget_for_settings(settings),
    }


def model_cost_micros(
    *,
    input_tokens: int,
    output_tokens: int,
    budget: dict[str, Any],
) -> int:
    input_rate = int(budget.get("input_cost_micros_per_1k", 0))
    output_rate = int(budget.get("output_cost_micros_per_1k", 0))
    return max(
        0,
        (input_tokens * input_rate + 999) // 1000 + (output_tokens * output_rate + 999) // 1000,
    )


def usage_totals(state: ResearchState) -> UsageTotals:
    input_tokens = 0
    output_tokens = 0
    cost_micros = 0
    for event in state.get("usage", []):
        input_tokens += int(event.get("input_tokens", 0))
        output_tokens += int(event.get("output_tokens", 0))
        cost_micros += int(event.get("cost_micros", 0))
    tool_calls = sum(int(result.get("tool_calls", 0)) for result in state.get("task_results", []))
    return UsageTotals(
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
        cost_micros=max(0, cost_micros),
        tool_calls=max(0, tool_calls),
    )


def budget_exhausted(state: ResearchState) -> bool:
    budget = state.get("budget", {})
    totals = usage_totals(state)
    max_cost = int(budget.get("max_cost_micros", 0))
    return (
        totals.input_tokens >= int(budget.get("max_input_tokens", 0))
        or totals.output_tokens >= int(budget.get("max_output_tokens", 0))
        or (max_cost > 0 and totals.cost_micros >= max_cost)
        or totals.tool_calls >= int(budget.get("max_tool_calls", 0))
    )


def latest_task_results(state: ResearchState) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for result in state.get("task_results", []):
        task_id = result.get("task_id")
        if isinstance(task_id, str):
            latest[task_id] = result
    return latest


def unique_findings(state: ResearchState) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in state.get("findings", []):
        key = finding.get("finding_key")
        if not isinstance(key, str) or key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def find_plan_task(state: ResearchState, task_id: str) -> dict[str, Any] | None:
    for task in state.get("plan", []):
        if task.get("task_id") == task_id:
            return task
    return None


def role_from_task(task: dict[str, Any]) -> AgentRole:
    role = task.get("agent_role")
    if isinstance(role, str):
        try:
            return AgentRole(role)
        except ValueError:
            pass
    raise ValueError("plan task has an invalid agent role")


__all__ = [
    "ResearchState",
    "UsageTotals",
    "budget_exhausted",
    "budget_for_settings",
    "find_plan_task",
    "initial_state",
    "latest_task_results",
    "model_cost_micros",
    "role_from_task",
    "unique_findings",
    "unresolved_errors",
    "usage_totals",
]


def unresolved_errors(state: ResearchState) -> list[dict[str, Any]]:
    """Errors not superseded by a later successful attempt of the same task."""
    latest = latest_task_results(state)
    unresolved: list[dict[str, Any]] = []
    for error in state.get("errors", []):
        task_id = error.get("task_id")
        if isinstance(task_id, str):
            result = latest.get(task_id)
            if result is not None and result.get("status") == "succeeded":
                continue
        unresolved.append(error)
    return unresolved
