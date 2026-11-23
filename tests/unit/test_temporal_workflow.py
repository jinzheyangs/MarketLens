from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflow.workflows import ResearchLifecycleWorkflow


async def test_research_lifecycle_workflow_drives_batches_and_review() -> None:
    state: dict[str, Any] = {
        "run_status": "pending",
        "review_status": None,
        "research_calls": 0,
        "review_calls": 0,
        "export_calls": 0,
    }

    @activity.defn(name="get_run_state")
    async def get_run_state(tenant_id: str, run_id: str) -> dict[str, object]:
        del tenant_id, run_id
        return {
            "run_status": state["run_status"],
            "review_status": state["review_status"],
            "pause_requested": False,
            "cancel_requested": False,
        }

    @activity.defn(name="run_research_batch")
    async def run_research_batch(tenant_id: str) -> int:
        del tenant_id
        state["research_calls"] = int(state["research_calls"]) + 1
        if state["research_calls"] >= 2:
            state["run_status"] = "completed"
            state["review_status"] = "pending"
        return 1

    @activity.defn(name="run_review_batch")
    async def run_review_batch(tenant_id: str) -> int:
        del tenant_id
        state["review_calls"] = int(state["review_calls"]) + 1
        if state["run_status"] == "completed":
            state["review_status"] = "completed"
        return 1

    @activity.defn(name="run_export_batch")
    async def run_export_batch(tenant_id: str) -> int:
        del tenant_id
        state["export_calls"] = int(state["export_calls"]) + 1
        return 0

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="test-research",
            workflows=[ResearchLifecycleWorkflow],
            activities=[
                get_run_state,
                run_research_batch,
                run_review_batch,
                run_export_batch,
            ],
        ),
    ):
        result = await env.client.execute_workflow(
            ResearchLifecycleWorkflow.run,
            {
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "run_id": "22222222-2222-2222-2222-222222222222",
                "poll_seconds": 1,
                "deadline_seconds": 60,
            },
            id="research-lifecycle-test",
            task_queue="test-research",
            execution_timeout=timedelta(seconds=60),
        )

    assert result["workflow_result"] == "completed"
    assert state["research_calls"] >= 2
    assert state["review_calls"] >= 1
    assert state["review_status"] == "completed"
