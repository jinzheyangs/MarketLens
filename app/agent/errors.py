"""调研 Agent 运行时抛出的、可分类的安全异常。"""

from __future__ import annotations


class AgentError(Exception):
    """Agent 异常基类：携带错误码、可安全对外的提示与是否可重试。"""

    code = "agent_error"
    safe_message = "The research operation failed."
    retryable = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.safe_message)


class ModelProviderError(AgentError):
    """模型供应商不可用，可重试。"""

    code = "model_provider_error"
    safe_message = "The model provider is unavailable."
    retryable = True


class InvalidModelResponseError(AgentError):
    """模型返回了无法解析/不符合约定的响应，不可重试。"""

    code = "invalid_model_response"
    safe_message = "The model provider returned an invalid response."
    retryable = False


class ToolNotAllowedError(AgentError):
    """当前 Agent 不被允许调用该工具，不可重试。"""

    code = "tool_not_allowed"
    safe_message = "The requested tool is not allowed for this agent."
    retryable = False


class ToolUnavailableError(AgentError):
    """研究工具当前不可用，可重试。"""

    code = "tool_unavailable"
    safe_message = "The requested research tool is unavailable."
    retryable = True


class ToolExecutionError(AgentError):
    """研究工具执行失败，可重试。"""

    code = "tool_execution_error"
    safe_message = "The research tool could not complete the request."
    retryable = True


class ResearchBudgetError(AgentError):
    """调研预算已耗尽，不可重试。"""

    code = "budget_exhausted"
    safe_message = "The research budget is exhausted."
    retryable = False


class RunControlRequestedError(AgentError):
    """调研运行被暂停或取消，不可重试。"""

    code = "run_control_requested"
    safe_message = "The research run was paused or canceled."
    retryable = False


class RunLeaseLostError(AgentError):
    """运行租约丢失（可能被其他 worker 接管），可重试。"""

    code = "lease_lost"
    safe_message = "The research run lease was lost."
    retryable = True
