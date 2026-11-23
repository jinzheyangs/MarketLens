"""Deterministic Temporal workflow for research and report lifecycle."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

_TERMINAL_RUN_STATUSES = {
    "completed",
    "partial",
    "failed",
    "canceled",
    "budget_exhausted",
}
_TERMINAL_REVIEW_STATUSES = {"completed", "failed"}


@workflow.defn(name="ResearchLifecycleWorkflow")
class ResearchLifecycleWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(payload["tenant_id"])
        run_id = str(payload["run_id"])
        poll_seconds = max(0.5, float(payload.get("poll_seconds", 5)))
        deadline_seconds = max(60, int(payload.get("deadline_seconds", 86_400)))
        started = workflow.now()
        last_state: dict[str, Any] = {}

        def expired() -> bool:
            return (workflow.now() - started).total_seconds() > deadline_seconds

        async def drive_once() -> None:
            await workflow.execute_activity(
                "run_research_batch",
                args=[tenant_id],
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.execute_activity(
                "run_review_batch",
                args=[tenant_id],
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.execute_activity(
                "run_export_batch",
                args=[tenant_id],
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        while not expired():
            state = await workflow.execute_activity(
                "get_run_state",
                args=[tenant_id, run_id],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            last_state = dict(state)
            status = str(state.get("run_status", ""))
            if status == "missing":
                return {**last_state, "workflow_result": "missing"}
            if status == "canceled":
                return {**last_state, "workflow_result": "canceled"}
            if status in _TERMINAL_RUN_STATUSES:
                break
            if status == "paused":
                await workflow.sleep(poll_seconds)
                continue
            await drive_once()
            await workflow.sleep(poll_seconds)

        while not expired():
            state = await workflow.execute_activity(
                "get_run_state",
                args=[tenant_id, run_id],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            last_state = dict(state)
            review_status = state.get("review_status")
            if review_status is None or review_status in _TERMINAL_REVIEW_STATUSES:
                return {**last_state, "workflow_result": "completed"}
            if state.get("run_status") == "canceled":
                return {**last_state, "workflow_result": "canceled"}
            await drive_once()
            await workflow.sleep(poll_seconds)

        return {**last_state, "workflow_result": "deadline_exceeded"}


__all__ = ["ResearchLifecycleWorkflow"]
