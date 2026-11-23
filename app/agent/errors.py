"""Safe, classified errors raised by the research agent runtime."""

from __future__ import annotations


class AgentError(Exception):
    code = "agent_error"
    safe_message = "The research operation failed."
    retryable = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.safe_message)


class ModelProviderError(AgentError):
    code = "model_provider_error"
    safe_message = "The model provider is unavailable."
    retryable = True


class InvalidModelResponseError(AgentError):
    code = "invalid_model_response"
    safe_message = "The model provider returned an invalid response."
    retryable = False


class ToolNotAllowedError(AgentError):
    code = "tool_not_allowed"
    safe_message = "The requested tool is not allowed for this agent."
    retryable = False


class ToolUnavailableError(AgentError):
    code = "tool_unavailable"
    safe_message = "The requested research tool is unavailable."
    retryable = True


class ToolExecutionError(AgentError):
    code = "tool_execution_error"
    safe_message = "The research tool could not complete the request."
    retryable = True


class ResearchBudgetError(AgentError):
    code = "budget_exhausted"
    safe_message = "The research budget is exhausted."
    retryable = False


class RunControlRequestedError(AgentError):
    code = "run_control_requested"
    safe_message = "The research run was paused or canceled."
    retryable = False


class RunLeaseLostError(AgentError):
    code = "lease_lost"
    safe_message = "The research run lease was lost."
    retryable = True
