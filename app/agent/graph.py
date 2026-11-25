"""LangGraph 主管（Supervisor）与专业 Agent 编排图。

编排图通过 LangGraph 的 fan-out（``Send``）实现有界并行调研，每个
superstep 后由 runner 持久化状态。图中不存在任意的 Agent 自由对话循环：
轮次、工具、发现条数与预算全部有上限约束。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from typing import Any, Protocol, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from app.agent.enums import (
    SPECIALIST_ROLES,
    AgentRole,
    ResearchPhase,
    StopReason,
    TaskStatus,
)
from app.agent.errors import AgentError
from app.agent.evidence import (
    EvidenceItem,
    ToolCollection,
    contains_prompt_injection,
    normalize_text,
    quote_is_supported,
)
from app.agent.providers import (
    AnalysisResult,
    ModelUsage,
    ResearchModel,
)
from app.agent.state import (
    ResearchState,
    budget_exhausted,
    latest_task_results,
    model_cost_micros,
    role_from_task,
    unique_findings,
    unresolved_errors,
    usage_totals,
)

logger = logging.getLogger(__name__)


class EvidenceToolbox(Protocol):
    """取证工具箱协议：按角色与问题收集授权范围内的证据。"""

    async def collect(
        self,
        *,
        role: AgentRole,
        question: str,
        tool_call_budget: int,
    ) -> ToolCollection: ...


def _usage_event(
    *,
    kind: str,
    role: AgentRole,
    usage: ModelUsage,
    budget: dict[str, Any],
) -> dict[str, Any]:
    """组装一次模型调用的用量事件（token、估算成本）。"""
    return {
        "kind": kind,
        "agent_role": role.value,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_micros": model_cost_micros(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            budget=budget,
        ),
    }


def _task_id(*, round_number: int, role: AgentRole, index: int, question: str) -> str:
    """生成确定性任务 ID：轮次 + 角色 + 序号 + 问题哈希，便于重试去重。"""
    digest = hashlib.sha256(normalize_text(question).casefold().encode()).hexdigest()[:16]
    return f"r{round_number}:{role.value}:{index}:{digest}"


def _pending_tasks(state: ResearchState) -> list[dict[str, Any]]:
    """找出待执行任务：尚未执行，或失败且可重试、未超最大尝试次数。"""
    latest = latest_task_results(state)
    max_attempts = int(state.get("budget", {}).get("max_task_attempts", 1))
    pending: list[dict[str, Any]] = []
    for task in state.get("plan", []):
        task_id = task.get("task_id")
        if not isinstance(task_id, str):
            continue
        result = latest.get(task_id)
        if result is None:
            pending.append(task)
            continue
        if (
            result.get("status") == TaskStatus.FAILED.value
            and bool(result.get("retryable", True))
            and int(result.get("attempts", 0)) < max_attempts
        ):
            pending.append(task)
    return pending


def _budget_slices(state: ResearchState, pending_count: int) -> dict[str, int] | None:
    """把剩余预算按待执行任务数切片；预算不足时返回 ``None``。"""
    if pending_count <= 0:
        return None
    budget = state.get("budget", {})
    totals = usage_totals(state)
    remaining_input = int(budget.get("max_input_tokens", 0)) - totals.input_tokens
    remaining_output = int(budget.get("max_output_tokens", 0)) - totals.output_tokens
    max_cost = int(budget.get("max_cost_micros", 0))
    remaining_cost = max_cost - totals.cost_micros if max_cost > 0 else 2**31 - 1
    remaining_tools = int(budget.get("max_tool_calls", 0)) - totals.tool_calls
    remaining_findings = int(budget.get("max_findings", 0)) - len(unique_findings(state))
    if remaining_input < pending_count or remaining_output < pending_count:
        return None
    if remaining_tools < pending_count or remaining_findings < pending_count:
        return None
    if max_cost > 0 and remaining_cost < pending_count:
        return None
    output_slice = min(
        int(budget.get("max_output_tokens_per_call", remaining_output)),
        remaining_output // pending_count,
    )
    return {
        "input_tokens": max(1, remaining_input // pending_count),
        "output_tokens": max(1, output_slice),
        "cost_micros": (max(1, remaining_cost // pending_count) if max_cost > 0 else 0),
        "tool_calls": max(
            1,
            min(
                int(budget.get("max_tool_calls_per_task", 1)),
                remaining_tools // pending_count,
            ),
        ),
        "findings": max(
            1,
            min(
                int(budget.get("max_findings_per_task", 1)),
                remaining_findings // pending_count,
            ),
        ),
    }


def _attempt_for(state: ResearchState, task_id: str) -> int:
    """计算某任务当前是第几次尝试。"""
    result = latest_task_results(state).get(task_id)
    if result is None:
        return 1
    return int(result.get("attempts", 0)) + 1


def _plan_task(
    *,
    round_number: int,
    index: int,
    role: AgentRole,
    question: str,
    focus: str | None,
) -> dict[str, Any]:
    """把模型规划出的提案转成图内标准任务定义。"""
    return {
        "task_id": _task_id(
            round_number=round_number,
            role=role,
            index=index,
            question=question,
        ),
        "agent_role": role.value,
        "question": question,
        "focus": focus,
        "round": round_number,
    }


def _finding_key(
    *,
    run_id: str,
    source_id: str,
    claim: str,
) -> str:
    """生成结论去重键：运行 + 来源 + 归一化结论的哈希。"""
    raw = f"{run_id}:{source_id}:{normalize_text(claim).casefold()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _validate_findings(
    *,
    run_id: str,
    task: dict[str, Any],
    role: AgentRole,
    proposals: AnalysisResult,
    evidence: Sequence[EvidenceItem],
    max_findings: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """校验模型产出的发现：引用必须在授权证据内、摘录必须被原文支撑，并去重。

    返回 (合法发现列表, 校验错误列表)。
    """
    ledger = {item.evidence_id: item for item in evidence}
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    if len(proposals.findings) > max_findings:
        return [], [
            {
                "code": "too_many_findings",
                "message": "The model returned more findings than the task budget allows.",
            }
        ]

    for proposal in proposals.findings:
        item = ledger.get(proposal.evidence_id)
        if item is None:
            errors.append(
                {
                    "code": "invalid_source_id",
                    "message": "The model cited evidence outside the authorized set.",
                }
            )
            continue
        if not quote_is_supported(proposal.evidence_quote, item.text):
            errors.append(
                {
                    "code": "unsupported_quote",
                    "message": "The model evidence quote was not found in the cited source.",
                }
            )
            continue
        claim = normalize_text(proposal.claim)
        key = _finding_key(
            run_id=run_id,
            source_id=str(item.source_id),
            claim=claim,
        )
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            {
                "finding_key": key,
                "task_key": str(task["task_id"]),
                "agent_role": role.value,
                "claim": claim,
                "evidence": proposal.evidence_quote,
                "source_kind": item.source_kind.value,
                "source_id": str(item.source_id),
                "document_id": str(item.document_id) if item.document_id else None,
                "document_version_id": (
                    str(item.document_version_id) if item.document_version_id else None
                ),
                "chunk_id": str(item.chunk_id) if item.chunk_id else None,
                "source_snapshot_id": (
                    str(item.source_snapshot_id) if item.source_snapshot_id else None
                ),
                "page_number": item.page_number,
                "paragraph_number": item.paragraph_number,
                "start_offset": item.start_offset,
                "end_offset": item.end_offset,
                "source_title": item.title[:500],
                "source_url": item.source_url,
                "observed_at": item.observed_at.isoformat(),
                "scope": normalize_text(proposal.scope)[:500],
                "limitations": normalize_text(proposal.limitations)[:500],
                "confidence": proposal.confidence,
                "injection_suspected": (
                    item.injection_suspected or contains_prompt_injection(proposal.evidence_quote)
                ),
                "evidence_untrusted": True,
            }
        )
    return findings, errors


def _failed_result(
    *,
    task: dict[str, Any],
    attempt: int,
    code: str,
    message: str,
    tool_calls: int,
    retryable: bool,
) -> dict[str, Any]:
    """组装一条失败任务结果记录。"""
    return {
        "task_id": str(task["task_id"]),
        "agent_role": str(task["agent_role"]),
        "status": TaskStatus.FAILED.value,
        "attempts": attempt,
        "findings_count": 0,
        "tool_calls": tool_calls,
        "error_code": code,
        "error_message": message,
        "retryable": retryable,
    }


def build_research_graph(
    *,
    model: ResearchModel,
    toolbox: EvidenceToolbox,
) -> CompiledStateGraph[ResearchState, None, ResearchState, ResearchState]:
    """构建并编译调研编排图。

    节点：主管规划 -> （并行）专业 Agent -> 汇总 -> 主管决策 -> 收尾。
    """

    async def supervisor_plan(state: ResearchState) -> dict[str, Any]:
        """主管规划节点：判断收尾/预算/重试，否则调用模型生成本轮任务计划。"""
        if state.get("stop_reason"):
            return {"phase": ResearchPhase.FINALIZING.value}
        if budget_exhausted(state):
            return {
                "phase": ResearchPhase.FINALIZING.value,
                "stop_reason": StopReason.BUDGET_EXHAUSTED.value,
            }

        current_plan = state.get("plan", [])
        if current_plan:
            pending = _pending_tasks(state)
            if pending:
                if _budget_slices(state, len(pending)) is None:
                    return {
                        "phase": ResearchPhase.FINALIZING.value,
                        "stop_reason": StopReason.BUDGET_EXHAUSTED.value,
                    }
                return {
                    "phase": ResearchPhase.DISPATCHING.value,
                    "request_new_plan": False,
                    "request_retry": False,
                }
            return {
                "phase": ResearchPhase.AGGREGATING.value,
                "aggregate_needed": True,
            }

        budget = state.get("budget", {})
        round_number = int(state.get("round", 0)) + 1
        if round_number > int(budget.get("max_rounds", 1)):
            return {
                "phase": ResearchPhase.FINALIZING.value,
                "stop_reason": StopReason.COMPLETED.value,
            }

        roles = tuple(
            AgentRole(value)
            for value in budget.get("agent_roles", [role.value for role in SPECIALIST_ROLES])
        )
        existing = [
            {
                "claim": str(finding.get("claim", ""))[:500],
                "source_id": str(finding.get("source_id", "")),
            }
            for finding in unique_findings(state)[-10:]
        ]
        totals = usage_totals(state)
        remaining_output = int(budget.get("max_output_tokens", 1)) - totals.output_tokens
        remaining_output = min(
            remaining_output,
            int(budget.get("max_output_tokens_per_call", remaining_output)),
        )
        max_tasks = min(
            int(budget.get("max_parallel_tasks", 1)),
            len(roles),
        )
        try:
            planned = await model.plan(
                objective=str(state.get("objective", "")),
                round_number=round_number,
                roles=roles,
                existing_findings=existing,
                max_tasks=max(1, max_tasks),
                max_output_tokens=max(1, remaining_output),
            )
        except AgentError as exc:
            return {
                "phase": ResearchPhase.FINALIZING.value,
                "stop_reason": StopReason.PROVIDER_ERROR.value,
                "errors": [{"code": exc.code, "message": exc.safe_message}],
            }
        except Exception:
            logger.exception("research_supervisor_plan_failed")
            return {
                "phase": ResearchPhase.FINALIZING.value,
                "stop_reason": StopReason.PROVIDER_ERROR.value,
                "errors": [
                    {
                        "code": "provider_error",
                        "message": "The research planner failed.",
                    }
                ],
            }

        usage_event = _usage_event(
            kind="plan",
            role=AgentRole.SUPERVISOR,
            usage=planned.usage,
            budget=budget,
        )
        tasks: list[dict[str, Any]] = []
        seen_questions: set[tuple[str, str]] = set()
        for index, proposal in enumerate(planned.tasks):
            if proposal.agent_role not in roles:
                continue
            key = (proposal.agent_role.value, normalize_text(proposal.question).casefold())
            if key in seen_questions:
                continue
            seen_questions.add(key)
            tasks.append(
                _plan_task(
                    round_number=round_number,
                    index=index,
                    role=proposal.agent_role,
                    question=proposal.question,
                    focus=proposal.focus,
                )
            )
            if len(tasks) >= int(budget.get("max_parallel_tasks", 1)):
                break

        next_state: dict[str, Any] = {
            "round": round_number,
            "round_start_findings": len(unique_findings(state)),
            "plan": tasks,
            "usage": [usage_event],
            "phase": ResearchPhase.DISPATCHING.value,
            "request_new_plan": False,
            "request_retry": False,
            "aggregate_needed": False,
            "stop_reason": None,
        }
        merged_raw = dict(state)
        merged_raw.update(next_state)
        merged = cast(ResearchState, merged_raw)
        if budget_exhausted(merged):
            next_state["stop_reason"] = StopReason.BUDGET_EXHAUSTED.value
            next_state["phase"] = ResearchPhase.FINALIZING.value
        elif not tasks:
            next_state["stop_reason"] = StopReason.NO_FINDINGS.value
            next_state["phase"] = ResearchPhase.FINALIZING.value
        elif _budget_slices(merged, len(tasks)) is None:
            next_state["stop_reason"] = StopReason.BUDGET_EXHAUSTED.value
            next_state["phase"] = ResearchPhase.FINALIZING.value
        return next_state

    async def specialist(payload: dict[str, Any]) -> dict[str, Any]:
        """专业 Agent 节点：取证 -> 模型分析 -> 校验发现，带超时与预算异常处理。"""
        task = payload["task"]
        role = role_from_task(task)
        attempt = int(payload.get("attempt", 1))
        task_budget = payload.get("task_budget", {})
        max_findings = int(payload.get("max_findings", 1))
        timeout_seconds = float(payload.get("task_timeout_seconds", 120))
        objective = str(payload.get("objective", ""))
        run_id = str(payload.get("run_id", ""))
        tool_calls = 0
        usage_events: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        try:
            async with asyncio.timeout(timeout_seconds):
                collection = await toolbox.collect(
                    role=role,
                    question=str(task["question"]),
                    tool_call_budget=int(task_budget.get("tool_calls", 0)),
                )
                tool_calls = collection.tool_calls
                for item in collection.errors:
                    errors.append(
                        {
                            "task_id": str(task["task_id"]),
                            "agent_role": role.value,
                            "code": item.get("code", "tool_error"),
                            "message": item.get("message", "A research tool failed."),
                        }
                    )
                evidence = collection.evidence
                if not evidence:
                    status = (
                        TaskStatus.FAILED.value if collection.errors else TaskStatus.SUCCEEDED.value
                    )
                    return {
                        "task_results": [
                            {
                                "task_id": str(task["task_id"]),
                                "agent_role": role.value,
                                "status": status,
                                "attempts": attempt,
                                "findings_count": 0,
                                "tool_calls": tool_calls,
                                "error_code": ("no_evidence" if collection.errors else None),
                                "error_message": (
                                    "No authorized evidence was available."
                                    if collection.errors
                                    else None
                                ),
                                "retryable": (True if collection.errors else None),
                            }
                        ],
                        "errors": errors,
                        "usage": usage_events,
                    }

                remaining_output = max(1, int(task_budget.get("output_tokens", 1)))
                analysis = await model.analyze(
                    objective=objective,
                    role=role,
                    question=str(task["question"]),
                    evidence=evidence,
                    max_findings=max_findings,
                    max_output_tokens=remaining_output,
                )
                usage_event = _usage_event(
                    kind="analysis",
                    role=role,
                    usage=analysis.usage,
                    budget=payload.get("budget", {}),
                )
                usage_events.append(usage_event)
                if (
                    analysis.usage.input_tokens > int(task_budget.get("input_tokens", 0))
                    or analysis.usage.output_tokens > int(task_budget.get("output_tokens", 0))
                    or (
                        int(task_budget.get("cost_micros", 0)) > 0
                        and int(usage_event["cost_micros"]) > int(task_budget.get("cost_micros", 0))
                    )
                ):
                    return {
                        "task_results": [
                            _failed_result(
                                task=task,
                                attempt=attempt,
                                code="budget_exhausted",
                                message="The task exceeded its model budget.",
                                tool_calls=tool_calls,
                                retryable=False,
                            )
                        ],
                        "errors": [
                            {
                                "task_id": str(task["task_id"]),
                                "agent_role": role.value,
                                "code": "budget_exhausted",
                                "message": "The task exceeded its model budget.",
                            }
                        ],
                        "usage": usage_events,
                    }

                findings, validation_errors = _validate_findings(
                    run_id=run_id,
                    task=task,
                    role=role,
                    proposals=analysis,
                    evidence=evidence,
                    max_findings=max_findings,
                )
                errors.extend(
                    {
                        "task_id": str(task["task_id"]),
                        "agent_role": role.value,
                        **item,
                    }
                    for item in validation_errors
                )
                status = (
                    TaskStatus.FAILED.value
                    if validation_errors and not findings
                    else TaskStatus.SUCCEEDED.value
                )
                return {
                    "findings": findings,
                    "task_results": [
                        {
                            "task_id": str(task["task_id"]),
                            "agent_role": role.value,
                            "status": status,
                            "attempts": attempt,
                            "findings_count": len(findings),
                            "tool_calls": tool_calls,
                            "error_code": (
                                "invalid_model_output"
                                if status == TaskStatus.FAILED.value
                                else None
                            ),
                            "error_message": (
                                "The model output failed citation validation."
                                if status == TaskStatus.FAILED.value
                                else None
                            ),
                            "retryable": (True if status == TaskStatus.FAILED.value else None),
                        }
                    ],
                    "errors": errors,
                    "usage": usage_events,
                }
        except TimeoutError:
            return {
                "task_results": [
                    _failed_result(
                        task=task,
                        attempt=attempt,
                        code="task_timeout",
                        message="The research task exceeded its time budget.",
                        tool_calls=tool_calls,
                        retryable=True,
                    )
                ],
                "errors": [
                    {
                        "task_id": str(task["task_id"]),
                        "agent_role": role.value,
                        "code": "task_timeout",
                        "message": "The research task exceeded its time budget.",
                    }
                ],
                "usage": usage_events,
            }
        except AgentError as exc:
            return {
                "task_results": [
                    _failed_result(
                        task=task,
                        attempt=attempt,
                        code=exc.code,
                        message=exc.safe_message,
                        tool_calls=tool_calls,
                        retryable=exc.retryable,
                    )
                ],
                "errors": [
                    {
                        "task_id": str(task["task_id"]),
                        "agent_role": role.value,
                        "code": exc.code,
                        "message": exc.safe_message,
                    }
                ],
                "usage": usage_events,
            }
        except Exception:
            logger.exception(
                "research_specialist_failed",
                extra={
                    "context": {
                        "task_id": str(task.get("task_id")),
                        "agent_role": role.value,
                    }
                },
            )
            return {
                "task_results": [
                    _failed_result(
                        task=task,
                        attempt=attempt,
                        code="internal_error",
                        message="The research agent failed.",
                        tool_calls=tool_calls,
                        retryable=True,
                    )
                ],
                "errors": [
                    {
                        "task_id": str(task["task_id"]),
                        "agent_role": role.value,
                        "code": "internal_error",
                        "message": "The research agent failed.",
                    }
                ],
                "usage": usage_events,
            }

    async def aggregate(state: ResearchState) -> dict[str, Any]:
        """汇总节点：各并行任务结果合并后进入决策阶段。"""
        return {
            "phase": ResearchPhase.DECIDING.value,
            "aggregate_needed": False,
        }

    async def supervisor_decision(state: ResearchState) -> dict[str, Any]:
        """主管决策节点：决定重试、补查一轮还是收尾（含部分失败判定）。"""
        if state.get("stop_reason"):
            return {"phase": ResearchPhase.FINALIZING.value}
        if budget_exhausted(state):
            return {
                "phase": ResearchPhase.FINALIZING.value,
                "stop_reason": StopReason.BUDGET_EXHAUSTED.value,
            }

        budget = state.get("budget", {})
        latest = latest_task_results(state)
        max_attempts = int(budget.get("max_task_attempts", 1))
        retryable = [
            result
            for result in latest.values()
            if result.get("status") == TaskStatus.FAILED.value
            and bool(result.get("retryable", True))
            and int(result.get("attempts", 0)) < max_attempts
        ]
        if retryable:
            return {
                "phase": ResearchPhase.REPLANNING.value,
                "request_retry": True,
            }

        finding_count = len(unique_findings(state))
        round_number = int(state.get("round", 0))
        if finding_count >= int(budget.get("max_findings", finding_count + 1)):
            failed = any(
                result.get("status") == TaskStatus.FAILED.value for result in latest.values()
            )
            return {
                "phase": ResearchPhase.FINALIZING.value,
                "stop_reason": (
                    StopReason.PARTIAL_FAILURE.value
                    if failed or unresolved_errors(state)
                    else StopReason.COMPLETED.value
                ),
            }
        new_findings = finding_count - int(state.get("round_start_findings", 0))
        if (
            round_number < int(budget.get("max_rounds", 1))
            and finding_count < int(budget.get("min_findings_for_followup", 0))
            and new_findings > 0
        ):
            return {
                "phase": ResearchPhase.REPLANNING.value,
                "request_new_plan": True,
                "plan": [],
            }

        failed = any(result.get("status") == TaskStatus.FAILED.value for result in latest.values())
        if finding_count == 0:
            stop_reason = StopReason.TASK_FAILURES.value if failed else StopReason.NO_FINDINGS.value
        elif failed or unresolved_errors(state):
            stop_reason = StopReason.PARTIAL_FAILURE.value
        else:
            stop_reason = StopReason.COMPLETED.value
        return {
            "phase": ResearchPhase.FINALIZING.value,
            "stop_reason": stop_reason,
        }

    async def finalize(state: ResearchState) -> dict[str, Any]:
        """收尾节点：置为已完成阶段并确定停止原因。"""
        return {
            "phase": ResearchPhase.FINALIZED.value,
            "stop_reason": state.get("stop_reason") or StopReason.COMPLETED.value,
        }

    def dispatch(state: ResearchState) -> list[Send] | str:
        """规划后的条件分发：无可执行任务则汇总/收尾，否则并行 Send 到专业 Agent。"""
        if state.get("stop_reason"):
            return "finalize"
        pending = _pending_tasks(state)
        if not pending:
            return "aggregate" if state.get("plan") else "finalize"
        slices = _budget_slices(state, len(pending))
        if slices is None:
            return "finalize"
        return [
            Send(
                "specialist",
                {
                    "task": task,
                    "task_budget": slices,
                    "attempt": _attempt_for(state, str(task["task_id"])),
                    "max_findings": int(slices["findings"]),
                    "task_timeout_seconds": float(
                        state.get("budget", {}).get("task_timeout_seconds", 120)
                    ),
                    "objective": str(state.get("objective", "")),
                    "run_id": str(state.get("run_id", "")),
                    "budget": state.get("budget", {}),
                },
            )
            for task in pending
        ]

    def after_decision(state: ResearchState) -> str:
        """决策后的条件边：需要重试/补查则回到规划，否则收尾。"""
        if state.get("stop_reason"):
            return "finalize"
        if state.get("request_new_plan") or state.get("request_retry"):
            return "supervisor_plan"
        return "finalize"

    graph = StateGraph(ResearchState)
    graph.add_node("supervisor_plan", supervisor_plan)
    graph.add_node("specialist", cast(Any, specialist))
    graph.add_node("aggregate", aggregate)
    graph.add_node("supervisor_decision", supervisor_decision)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "supervisor_plan")
    graph.add_conditional_edges(
        "supervisor_plan",
        dispatch,
        ["specialist", "aggregate", "finalize"],
    )
    graph.add_edge("specialist", "aggregate")
    graph.add_edge("aggregate", "supervisor_decision")
    graph.add_conditional_edges(
        "supervisor_decision",
        after_decision,
        ["supervisor_plan", "finalize"],
    )
    graph.add_edge("finalize", END)
    return graph.compile()


__all__ = ["EvidenceToolbox", "build_research_graph"]
