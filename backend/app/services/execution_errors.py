"""Execution 领域错误（Phase 6A + 6B + 6C）。

所有错误携带稳定错误码（EXEC_ 前缀，与 TASK_/DOCUMENT_/EMBEDDING_ 域分离）；
HTTP 状态码由 6E 的 main.py 统一映射（本阶段无 API，仅登记映射供测试与
未来路由使用）。文案全部 sanitized：不含堆栈、路径、API Key、数据库 URL、
原始异常细节或完整敏感输入。

错误码一览（对客户端稳定，前端/测试依赖）：
- EXEC_NOT_FOUND                 404  执行不存在
- EXEC_INVALID_STATE_TRANSITION  409  非法状态转换（含终态不可逆）
- EXEC_IDEMPOTENCY_CONFLICT      409  同幂等键但请求不同
- EXEC_TYPE_UNKNOWN              422  execution_type 不在注册表
- EXEC_PAYLOAD_INVALID           422  payload 含白名单外/敏感字段或非对象
- EXEC_IDEMPOTENCY_KEY_INVALID   422  幂等键长度/字符集不合法
- EXEC_CLAIM_LOST                409  租约丢失（renew/finish 时 token 不匹配
                                      或已过期，需终止处理）
- EXEC_WORKER_CONFLICT           409  第二个 Worker 试图与活跃租约共存
                                      （SQLite 单 Worker 限制）
- EXEC_TOOL_UNKNOWN              422  步骤引用的工具不在工具注册表（fail-closed）
- EXEC_TOOL_NOT_ALLOWED          500  工具已注册但被禁用（enabled=False 隔离）
- EXEC_STEP_INVALID              422  步骤键不存在/非法/终态执行不可编排
- EXEC_STEP_CONFLICT             409  步骤物化冲突（模板漂移：同索引不同键）
- EXEC_STEP_REPLAY_UNSAFE        409  崩溃恢复时工具不可安全重放（拒绝自动重放）
- EXEC_CONFIRMATION_REQUIRED     409  需要确认且无有效 GRANTED 记录（等待门）
- EXEC_CONFIRMATION_REJECTED     409  确认被拒绝（稳定失败，不重试）
- EXEC_CONFIRMATION_EXPIRED      409  确认已过期（输入变化，需重新确认）
- EXEC_SCHEMA_INVALID            422  输入/输出 schema 校验失败（unknown
                                      fields 默认拒绝）
- EXEC_EVENT_CONFLICT            409  事件序列号冲突（并发裁决，事务回滚）
- EXEC_RUN_AT_INVALID            422  run_at 非法（非 datetime / 类型错误）
- EXEC_RUN_AT_NAIVE             422  run_at 为 naive datetime（拒绝，绝不
                                      静默解释为本地或 UTC 时间）
- EXEC_MISFIRE_EXPIRED          409  misfire FAIL 策略的终态错误码（execution
                                      超过 run_at + grace 后按策略判死）
"""

# 错误码 → HTTP 状态（6E 的全局 handler 使用；前端依赖这些状态码）
EXECUTION_ERROR_STATUS: dict[str, int] = {
    "EXEC_NOT_FOUND": 404,
    "EXEC_INVALID_STATE_TRANSITION": 409,
    "EXEC_IDEMPOTENCY_CONFLICT": 409,
    "EXEC_CLAIM_LOST": 409,
    "EXEC_WORKER_CONFLICT": 409,
    "EXEC_TOOL_UNKNOWN": 422,
    "EXEC_TOOL_NOT_ALLOWED": 500,
    "EXEC_STEP_INVALID": 422,
    "EXEC_STEP_CONFLICT": 409,
    "EXEC_STEP_REPLAY_UNSAFE": 409,
    "EXEC_CONFIRMATION_REQUIRED": 409,
    "EXEC_CONFIRMATION_REJECTED": 409,
    "EXEC_CONFIRMATION_EXPIRED": 409,
    "EXEC_SCHEMA_INVALID": 422,
    "EXEC_EVENT_CONFLICT": 409,
    "EXEC_RUN_AT_INVALID": 422,
    "EXEC_RUN_AT_NAIVE": 422,
    "EXEC_MISFIRE_EXPIRED": 409,
    "EXEC_TYPE_UNKNOWN": 422,
    "EXEC_PAYLOAD_INVALID": 422,
    "EXEC_IDEMPOTENCY_KEY_INVALID": 422,
    "ACTION_CONFIRM_TIME_NOT_FUTURE": 422,
    "ACTION_CONFIRM_RESULT_MISSING": 500,
}


class ExecutionError(Exception):
    """Execution 领域错误基类。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class ExecutionNotFoundError(ExecutionError):
    def __init__(self, execution_id: int):
        super().__init__(
            "EXEC_NOT_FOUND", f"Execution {execution_id} does not exist"
        )


class InvalidExecutionTransitionError(ExecutionError):
    """非法状态转换（含终态不可逆）。message 脱敏：只含状态值与 id。"""

    def __init__(self, execution_id: int, current: str, target: str):
        super().__init__(
            "EXEC_INVALID_STATE_TRANSITION",
            f"Execution {execution_id} cannot transition from "
            f"{current} to {target}",
        )


class IdempotencyConflictError(ExecutionError):
    """同幂等键但规范化请求不同：稳定 409（含已有 execution_id 供排查）。"""

    def __init__(self, execution_id: int, key: str):
        super().__init__(
            "EXEC_IDEMPOTENCY_CONFLICT",
            f"Idempotency-Key {key!r} was already used for Execution "
            f"{execution_id} with a different request",
        )


class ExecutionTypeUnknownError(ExecutionError):
    def __init__(self, execution_type: str):
        super().__init__(
            "EXEC_TYPE_UNKNOWN",
            f"Unknown execution_type {execution_type!r}",
        )


class ExecutionPayloadError(ExecutionError):
    """payload 校验失败：非对象、白名单外字段或敏感字段名。"""

    def __init__(self, message: str):
        super().__init__("EXEC_PAYLOAD_INVALID", message)


class InvalidIdempotencyKeyError(ExecutionError):
    def __init__(self, message: str):
        super().__init__("EXEC_IDEMPOTENCY_KEY_INVALID", message)


class ActionConfirmTimeError(ExecutionError):
    def __init__(self, field_name: str):
        super().__init__(
            "ACTION_CONFIRM_TIME_NOT_FUTURE",
            f"{field_name} must be a future datetime",
        )


class ActionConfirmResultMissingError(ExecutionError):
    def __init__(self):
        super().__init__(
            "ACTION_CONFIRM_RESULT_MISSING",
            "Confirmed action result is unavailable",
        )


class ExecutionClaimLostError(ExecutionError):
    """租约丢失：renew/finish 时 lease_token 不匹配或已过期。

    Worker 必须立即终止对该 execution 的处理（租约已被回收或移交），
    绝不继续写状态。
    """

    def __init__(self, execution_id: int, attempt_id: int):
        super().__init__(
            "EXEC_CLAIM_LOST",
            f"Lease lost for Execution {execution_id} attempt {attempt_id}",
        )


class ExecutionWorkerConflictError(ExecutionError):
    """第二个 Worker 与现存活跃租约共存（SQLite 单 Worker 限制）。"""

    def __init__(self, worker_id: str, lease_owner: str):
        super().__init__(
            "EXEC_WORKER_CONFLICT",
            f"Worker {worker_id!r} cannot start: active lease held by "
            f"Worker {lease_owner!r} (single-worker database)",
        )


class ExecutionStepError(ExecutionError):
    """步骤编排错误基类（Phase 6C）。

    携带 retryable 标志：决定 Worker 失败路径走 attempt 重试还是稳定
    FAILED。step_key 供日志定位（消息中不出现用户数据）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        step_key: str | None = None,
    ):
        self.retryable = retryable
        self.step_key = step_key
        super().__init__(code, message)


class ToolUnknownError(ExecutionStepError):
    """工具不在注册表（fail-closed）：配置错误，稳定失败不重试。"""

    def __init__(self, tool_name: str):
        super().__init__(
            "EXEC_TOOL_UNKNOWN",
            f"Tool {tool_name!r} is not registered; refusing to execute",
            retryable=False,
        )


class ToolNotAllowedError(ExecutionStepError):
    """工具已注册但被禁用（enabled=False 隔离）：配置错误，稳定失败。"""

    def __init__(self, tool_name: str):
        super().__init__(
            "EXEC_TOOL_NOT_ALLOWED",
            f"Tool {tool_name!r} is registered but not allowed to run",
            retryable=False,
        )


class StepInvalidError(ExecutionStepError):
    """步骤无效：键不存在/非法、模板为空、终态 execution 不可编排。"""

    def __init__(self, message: str):
        super().__init__("EXEC_STEP_INVALID", message, retryable=False)


class StepConflictError(ExecutionStepError):
    """步骤物化冲突：同 (execution, step_index) 已被不同 step_key 占用
    （模板漂移）。数据库唯一约束裁决后触发，稳定失败。"""

    def __init__(self, execution_id: int, step_index: int):
        super().__init__(
            "EXEC_STEP_CONFLICT",
            f"Step conflict at execution {execution_id} index {step_index}",
            retryable=False,
        )


class StepReplayUnsafeError(ExecutionStepError):
    """RUNNING 步骤遇 Worker 崩溃且工具不可安全重放：拒绝自动重放，
    稳定失败（或人工确认，本阶段取稳定失败路径）。"""

    def __init__(self, step_key: str, tool_name: str):
        super().__init__(
            "EXEC_STEP_REPLAY_UNSAFE",
            f"Step {step_key!r} (tool {tool_name!r}) cannot be safely "
            "replayed after an interrupted attempt",
            retryable=False,
            step_key=step_key,
        )


class ConfirmationRequiredError(ExecutionStepError):
    """确认门未放行（无有效 GRANTED）：Worker 不得执行。
    可重试——execution 退回队列等待确认（受 max_attempts 上限约束）。"""

    def __init__(self, step_key: str, tool_name: str):
        super().__init__(
            "EXEC_CONFIRMATION_REQUIRED",
            f"Step {step_key!r} (tool {tool_name!r}) requires confirmation "
            "before execution",
            retryable=True,
            step_key=step_key,
        )


class ConfirmationRejectedError(ExecutionStepError):
    """确认被拒绝：稳定失败，绝不重试。"""

    def __init__(self, step_key: str, tool_name: str):
        super().__init__(
            "EXEC_CONFIRMATION_REJECTED",
            f"Confirmation for step {step_key!r} (tool {tool_name!r}) "
            "was rejected",
            retryable=False,
            step_key=step_key,
        )


class ConfirmationExpiredError(ExecutionStepError):
    """确认已过期（输入 hash 变化后旧确认失效）：需重新确认，可重试。"""

    def __init__(self, step_key: str, tool_name: str):
        super().__init__(
            "EXEC_CONFIRMATION_EXPIRED",
            f"Confirmation for step {step_key!r} (tool {tool_name!r}) "
            "has expired; re-confirmation required",
            retryable=True,
            step_key=step_key,
        )


class StepSchemaError(ExecutionStepError):
    """输入/输出 schema 校验失败（unknown fields 默认拒绝）。
    输入源自不可变 payload / 静态模板——重试不会改变结果，稳定失败。"""

    def __init__(self, message: str):
        super().__init__("EXEC_SCHEMA_INVALID", message, retryable=False)


class EventConflictError(ExecutionError):
    """事件序列号冲突（并发写同一 execution 的事件流）。事务已回滚，
    调用方按失败路径处理。"""

    def __init__(self, execution_id: int, event_type: str):
        super().__init__(
            "EXEC_EVENT_CONFLICT",
            f"Event sequence conflict for Execution {execution_id} "
            f"({event_type}); transaction rolled back",
        )


class ExecutionRunAtInvalidError(ExecutionError):
    """run_at 非法：非 datetime 实例等类型错误。文案脱敏，不含原始值。"""

    def __init__(self):
        super().__init__(
            "EXEC_RUN_AT_INVALID",
            "run_at must be a datetime instance",
        )


class ExecutionRunAtNaiveError(ExecutionError):
    """run_at 为 naive datetime：拒绝，绝不静默解释为本地或 UTC 时间。
    调用方必须提供 aware datetime（推荐 timezone.utc）。"""

    def __init__(self):
        super().__init__(
            "EXEC_RUN_AT_NAIVE",
            "run_at must include timezone information (naive datetime "
            "rejected; supply an aware datetime, preferably UTC)",
        )


# FAIL misfire 策略写入 execution.last_error_message 的稳定脱敏文案
# （对应错误码 EXEC_MISFIRE_EXPIRED）。
MISFIRE_EXPIRED_MESSAGE = (
    "Execution misfired: now exceeded run_at + misfire_grace_seconds; "
    "failed per the type's misfire policy"
)
