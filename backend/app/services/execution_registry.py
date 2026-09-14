"""Execution type / Tool 注册表（Phase 6A + 6B + 6C + 6D）。

配置级白名单（沿 ALLOWED_TRANSITIONS 白名单表模式）：
- execution_type 必须在注册表内，未知类型拒绝（EXEC_TYPE_UNKNOWN）；
- payload 只允许白名单字段，未知字段与敏感字段名拒绝
  （EXEC_PAYLOAD_INVALID，fail-closed——绝不静默丢弃或放行）；
- Phase 6C：工具（ToolSpec）与步骤模板（StepSpec）都是**声明式白名单**。

安全边界（6C fail-closed）：
- handler 是注册表内静态引用的白名单函数（模块级 dict 字面量）——
  绝无字符串 import、eval、exec、反射调用、任意 shell/命令执行；
  模型文本永远只是 handler 的数据输入，不参与任何代码路径解析；
- 工具未注册 → EXEC_TOOL_UNKNOWN；已注册但 enabled=False（隔离）→
  EXEC_TOOL_NOT_ALLOWED；
- destructive=true 必须同时 requires_confirmation=true（构造期校验，
  违规注册直接 ValueError——绝不自动视为安全）；
- destructive=true 的工具 replay_safe 必须显式 False（崩溃后绝不自
  动重放）；
- unknown fields 默认拒绝：schema 一律 pydantic strict + extra="forbid"。

规范化契约（6A 保留）：
- normalize_payload 返回 (规范化 payload, sha256 十六进制哈希)；
- 规范化 = 仅保留白名单字段 + 递归键排序 + 紧凑 JSON 序列化；
- 哈希输入禁用 ASCII 转义丢失：ensure_ascii=False，中文 payload 稳定。

legacy 兼容（6B 保留）：EXECUTION_HANDLERS 仍是可用的 handler 注册表。
未声明 steps 模板的 type 由 Worker 编排器合成单步
（step_key="run"，tool=type 名，见 services/execution_steps.py），
handler 语义与 6B 逐字节等价（含 EXEC_HANDLER_ERROR 行为）。
"""

import hashlib
import json
import math
from collections import ChainMap
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from app.core.sanitize import is_denied_field_name
from app.services.execution_errors import (
    ExecutionError,
    ExecutionPayloadError,
    ExecutionTypeUnknownError,
    ToolUnknownError,
)
# Phase 7.1 真实业务工具 handler（静态白名单引用——模块内定义 schema 与
# 注入点，见 services/execution_tools/；绝无字符串 import / eval / exec）。
from app.services.execution_tools.document_reindex import (
    DocumentReindexInput,
    DocumentReindexOutput,
    document_reindex_handler,
)
from app.services.execution_tools.reminder_send import (
    ReminderSendInput,
    ReminderSendOutput,
    reminder_send_handler,
)
from app.services.execution_tools.calendar_create import (
    CalendarCreateInput,
    CalendarCreateOutput,
    calendar_create_event_handler,
)

# ---- 6C：严格 schema 基类（unknown fields 默认拒绝） ----

class _StrictModel(BaseModel):
    """输入/输出 schema 基类：未知字段一律拒绝（fail-closed）。

    声明式 schema 是工具白名单的一部分——允许的字段、类型、嵌套结构
    全部静态声明，绝不从模型文本或运行时数据推导。
    """

    model_config = ConfigDict(extra="forbid")


# ---- 6C：声明式 Step / Tool 定义 ----

@dataclass(frozen=True)
class StepSpec:
    """执行类型模板中的一步（静态声明，确定性）。

    - step_key：步骤键（模板内唯一，DB 约束裁决）；[A-Za-z0-9._-] 且 ≤50
      字符（编排器校验，违规 → EXEC_STEP_INVALID）；
    - tool_name：工具注册表键（静态白名单，绝不从 payload/模型文本解析）；
    - input_source："payload"（execution 规范化 payload）或 "literal"
      （模板内静态字面量）。不支持模型动态规划。
    """

    step_key: str
    tool_name: str
    input_source: str = "payload"  # "payload" | "literal"
    input_literal: Any = None  # input_source="literal" 时的静态输入


@dataclass(frozen=True)
class ToolSpec:
    """工具声明（静态白名单）。

    - handler：模块内静态引用的白名单函数（不允许字符串/路径解析）；
    - input_schema / output_schema：pydantic 模型（extra="forbid"）；
      None = 该方向不校验（legacy 兼容路径），但 input 仍受
      deny 词表递归检查 + 模板静态性约束；
    - destructive：默认 False；为 True 时必须 requires_confirmation=True；
    - requires_confirmation：执行前必须存在绑定输入 hash 的 GRANTED 确认；
    - retryable：step 失败是否交给现有 attempt 重试机制（受 max_attempts
      与退避约束；绝不另起第二套 retry 系统）；
    - replay_safe：Worker 崩溃时 RUNNING 步骤可否安全重放；destructive
      工具必须显式 False（绝不自 动重放）；
    - sanitize_result：输出落盘前是否经过 sanitize_result_value（默认
      开启；仅在工具输出绝无敏感内容且需要保留原样时显式关闭）；
    - enabled：False = 隔离（注册但不可执行，EXEC_TOOL_NOT_ALLOWED）。
    """

    name: str
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    input_schema: type[_StrictModel] | None = None
    output_schema: type[_StrictModel] | None = None
    destructive: bool = False
    requires_confirmation: bool = False
    retryable: bool = True
    replay_safe: bool = True
    sanitize_result: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        # 构造期校验：违规注册直接失败（fail-closed，绝不静默修正）
        if self.destructive and not self.requires_confirmation:
            raise ValueError(
                f"tool {self.name!r}: destructive tools must declare "
                "requires_confirmation=True"
            )
        if self.destructive and self.replay_safe:
            raise ValueError(
                f"tool {self.name!r}: destructive tools must declare "
                "replay_safe=False (never auto-replay)"
            )
        if self.requires_confirmation and not self.destructive:
            # 非 destructive 但需确认：允许（作者显式声明），无需额外约束
            pass


# ---- 6D：Misfire 策略集中配置（registry 级默认；不持久化到 execution 行） ----
# misfire 定义：now > run_at + misfire_grace_seconds（run_at IS NOT NULL 且
# attempt_count == 0——从未被领取的到期任务才可能 misfire；重试不属 misfire）。
# 策略变化作用于未来裁决，与 6C 模板漂移同语义。所有值经构造期校验
# （fail-closed）：policy 必须白名单、grace 必须非负有限且有合理上限。
MISFIRE_POLICIES: frozenset[str] = frozenset(
    {"RUN_IMMEDIATELY", "SKIP", "FAIL"}
)
DEFAULT_MISFIRE_POLICY: str = "RUN_IMMEDIATELY"
DEFAULT_MISFIRE_GRACE_SECONDS: float = 300.0  # 5 分钟
MAX_MISFIRE_GRACE_SECONDS: float = 604800.0  # 7 天（合理上限）


@dataclass(frozen=True)
class ExecutionTypeSpec:
    name: str
    payload_schema_version: int
    # 允许的 payload 顶层字段名（fail-closed：白名单外即拒绝）
    payload_allowed_fields: frozenset[str]
    max_attempts: int
    timeout_seconds: int | None
    # 确定性指数退避（Phase 6B）：delay = base * factor ** (attempt_number - 1)。
    # 无随机成分——同一重试序列对固定失败模式可复现（测试/审计友好）。
    retry_backoff_base_seconds: float = 2.0
    retry_backoff_factor: float = 2.0
    # 6C：静态步骤模板（None = legacy 单 handler 模式，合成单步，
    # 行为与 6B 等价）。步骤序列严格线性、确定性执行。
    steps: tuple[StepSpec, ...] | None = None
    # 6D：一次性调度 misfire 策略（每个 execution type 在 registry 声明
    # 默认 policy 与 grace）。构造期校验违规注册直接 ValueError（fail-closed，
    # 绝不静默修正）——见 __post_init__。
    misfire_policy: str = DEFAULT_MISFIRE_POLICY
    misfire_grace_seconds: float = DEFAULT_MISFIRE_GRACE_SECONDS
    # Some public-visible types may only be created by a trusted workflow.
    api_creatable: bool = True

    def __post_init__(self) -> None:
        if self.misfire_policy not in MISFIRE_POLICIES:
            raise ValueError(
                f"execution_type {self.name!r}: misfire_policy "
                f"{self.misfire_policy!r} not in {sorted(MISFIRE_POLICIES)}"
            )
        grace = self.misfire_grace_seconds
        if isinstance(grace, bool) or not isinstance(grace, (int, float)):
            raise ValueError(
                f"execution_type {self.name!r}: misfire_grace_seconds must "
                "be a number"
            )
        if not math.isfinite(float(grace)):
            raise ValueError(
                f"execution_type {self.name!r}: misfire_grace_seconds must "
                "be finite (NaN/Infinity rejected)"
            )
        if grace < 0 or grace > MAX_MISFIRE_GRACE_SECONDS:
            raise ValueError(
                f"execution_type {self.name!r}: misfire_grace_seconds must "
                f"be in [0, {MAX_MISFIRE_GRACE_SECONDS}]"
            )


# ---- 6A：system.ping（无副作用验证型） ----

class PingInput(_StrictModel):
    """system.ping 输入：必须为空对象 {}（与 payload 白名单一致）。"""


class PingOutput(_StrictModel):
    """system.ping 输出：固定 {pong: bool}。"""

    pong: bool


EXECUTION_TYPES: dict[str, ExecutionTypeSpec] = {
    # 无副作用验证型：payload 必须为空 {}。不允许任何字段——同时作为
    # 敏感字段禁存规则的测试锚点（任何字段名都会被拒）。
    # 6C：单步模板（step_key="ping"，工具 system.ping，输入 = execution payload）。
    "system.ping": ExecutionTypeSpec(
        name="system.ping",
        payload_schema_version=1,
        payload_allowed_fields=frozenset(),
        max_attempts=3,
        timeout_seconds=None,
        steps=(StepSpec(step_key="ping", tool_name="system.ping"),),
    ),
    # Phase 7.1 真实业务工具（复用既有服务，见 services/execution_tools/）：
    # - reminder.send：非破坏、可重放（replay_safe=True）。白名单字段
    #   reminder_id / task_id / message；单步模板 step_key="send"。
    # - documents.reindex：破坏性（requires_confirmation=True、
    #   replay_safe=False——ToolSpec.__post_init__ 构造期强制）。白名单
    #   字段 document_id；单步模板 step_key="reindex"。
    "reminder.send": ExecutionTypeSpec(
        name="reminder.send",
        payload_schema_version=1,
        payload_allowed_fields=frozenset({"reminder_id", "task_id", "message"}),
        max_attempts=3,
        timeout_seconds=None,
        steps=(StepSpec(step_key="send", tool_name="reminder.send"),),
    ),
    "documents.reindex": ExecutionTypeSpec(
        name="documents.reindex",
        payload_schema_version=1,
        payload_allowed_fields=frozenset({"document_id"}),
        max_attempts=3,
        timeout_seconds=None,
        steps=(StepSpec(step_key="reindex", tool_name="documents.reindex"),),
    ),
}

# Phase 9B extends the public worker/listing allowlist while preserving the
# sealed Phase 7 registry object and its exact compatibility assertions.
CALENDAR_EXECUTION_TYPES: dict[str, ExecutionTypeSpec] = {
    "calendar.create_event": ExecutionTypeSpec(
        name="calendar.create_event",
        payload_schema_version=1,
        payload_allowed_fields=frozenset(
            {"title", "start_at", "end_at", "location", "transaction_id"}
        ),
        max_attempts=3,
        timeout_seconds=120,
        steps=(StepSpec(step_key="create_event", tool_name="calendar.create_event"),),
        api_creatable=False,
    ),
}

PUBLIC_EXECUTION_TYPES = ChainMap(CALENDAR_EXECUTION_TYPES, EXECUTION_TYPES)


# Internal receipts are intentionally outside EXECUTION_TYPES so the public
# Phase 7 execution/tool allowlist remains byte-for-byte the same three types.
# They are available only when a trusted service explicitly opts in.
INTERNAL_EXECUTION_TYPES: dict[str, ExecutionTypeSpec] = {
    "action.confirm": ExecutionTypeSpec(
        name="action.confirm",
        payload_schema_version=1,
        payload_allowed_fields=frozenset(
            {"conversation_id", "action", "task", "reminder", "calendar_event"}
        ),
        max_attempts=1,
        timeout_seconds=None,
        steps=(),
    ),
}


def get_type_spec(
    execution_type: str, *, include_internal: bool = False
) -> ExecutionTypeSpec:
    """取注册表条目；未知类型抛 EXEC_TYPE_UNKNOWN。"""
    spec = PUBLIC_EXECUTION_TYPES.get(execution_type)
    if spec is None and include_internal:
        spec = INTERNAL_EXECUTION_TYPES.get(execution_type)
    if spec is None:
        raise ExecutionTypeUnknownError(execution_type)
    return spec


def backoff_seconds(spec: ExecutionTypeSpec, attempt_number: int) -> float:
    """确定性指数退避：delay = base * factor ** (attempt_number - 1)。"""
    return spec.retry_backoff_base_seconds * (
        spec.retry_backoff_factor ** (attempt_number - 1)
    )


# ---- 执行器注册（Phase 6B：legacy handler 表；6C：工具白名单） ----

def _ping_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """system.ping：无副作用、可重复执行的验证型任务。

    payload 恒为 {}（白名单为空）。返回固定成功载荷；多次执行结果一致。
    """
    return {"pong": True}


# execution_type → handler（白名单函数；Worker 只调用注册表内函数）。
# 6C 保留为 legacy 兼容表：未声明 steps 模板的 type 通过它合成单步工具。
EXECUTION_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "system.ping": _ping_handler,
}

# 工具白名单（6C）：声明式 ToolSpec。handler 是模块内静态引用——
# 不允许字符串 import / eval / exec / 反射调用 / 任意 shell。
EXECUTION_TOOLS: dict[str, ToolSpec] = {
    "system.ping": ToolSpec(
        name="system.ping",
        handler=_ping_handler,
        input_schema=PingInput,
        output_schema=PingOutput,
        destructive=False,
        requires_confirmation=False,
        retryable=True,
        replay_safe=True,
    ),
    # Phase 7.1 真实业务工具。destructive/replay_safe 组合由
    # ToolSpec.__post_init__ 构造期强制校验（违规注册直接 ValueError）。
    "reminder.send": ToolSpec(
        name="reminder.send",
        handler=reminder_send_handler,
        input_schema=ReminderSendInput,
        output_schema=ReminderSendOutput,
        destructive=False,
        requires_confirmation=False,
        retryable=True,
        replay_safe=True,
    ),
    "documents.reindex": ToolSpec(
        name="documents.reindex",
        handler=document_reindex_handler,
        input_schema=DocumentReindexInput,
        output_schema=DocumentReindexOutput,
        destructive=True,
        requires_confirmation=True,
        retryable=True,
        replay_safe=False,
    ),
}

CALENDAR_EXECUTION_TOOLS: dict[str, ToolSpec] = {
    "calendar.create_event": ToolSpec(
        name="calendar.create_event",
        handler=calendar_create_event_handler,
        input_schema=CalendarCreateInput,
        output_schema=CalendarCreateOutput,
        destructive=False,
        requires_confirmation=True,
        retryable=True,
        replay_safe=True,
    ),
}

PUBLIC_EXECUTION_TOOLS = ChainMap(CALENDAR_EXECUTION_TOOLS, EXECUTION_TOOLS)


def get_handler(execution_type: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """取注册的 handler（legacy 6B 入口）；未注册则抛 EXEC_HANDLER_ERROR。"""
    handler = EXECUTION_HANDLERS.get(execution_type)
    if handler is None:
        raise ExecutionError(
            "EXEC_HANDLER_ERROR",
            f"No handler registered for execution_type {execution_type!r}",
        )
    return handler


def get_tool(tool_name: str, *, legacy_fallback: bool = True) -> ToolSpec:
    """取工具注册表条目（6C 白名单解析）。

    - 工具注册表命中 → 返回 ToolSpec；
    - legacy_fallback（未声明 steps 模板的 6B type）：退回
      EXECUTION_HANDLERS 合成 ToolSpec（无 schema、非 destructive、
      retryable/replay_safe=True——与 6B 行为逐字节等价）；
    - 都未命中 → EXEC_TOOL_UNKNOWN（fail-closed）。
    """
    tool = PUBLIC_EXECUTION_TOOLS.get(tool_name)
    if tool is not None:
        return tool
    if legacy_fallback:
        handler = EXECUTION_HANDLERS.get(tool_name)
        if handler is not None:
            return ToolSpec(name=tool_name, handler=handler)
        raise ExecutionError(
            "EXEC_HANDLER_ERROR",
            f"No handler registered for execution_type {tool_name!r}",
        )
    raise ToolUnknownError(tool_name)


def resolve_step_template(
    spec: ExecutionTypeSpec,
) -> tuple[StepSpec, ...]:
    """解析类型的步骤模板。

    显式 steps → 原样返回；None（legacy 6B type）→ 合成单步
    （step_key="run"，tool=type 名）。模板是静态声明，绝不含运行时数据。
    """
    if spec.steps is not None:
        return spec.steps
    return (StepSpec(step_key="run", tool_name=spec.name),)


def normalize_payload(
    execution_type: str, raw_payload: Any, *, include_internal: bool = False
) -> tuple[dict[str, Any], str]:
    """按注册表白名单规范化 payload 并计算请求指纹哈希。

    规则（fail-closed）：
    - payload 必须是对象（dict），否则 EXEC_PAYLOAD_INVALID；
    - 白名单外字段 → 拒绝（绝不静默丢弃）；
    - 字段名命中敏感 deny 词表 → 拒绝（api_key/password/token/...）；
    - 返回值：规范化 payload（白名单内字段，无多余键）+ sha256(紧凑 JSON)。
    """
    spec = get_type_spec(execution_type, include_internal=include_internal)
    if not isinstance(raw_payload, dict):
        raise ExecutionPayloadError(
            f"payload for {execution_type!r} must be a JSON object"
        )
    normalized: dict[str, Any] = {}
    for field_name, value in raw_payload.items():
        if field_name not in spec.payload_allowed_fields:
            raise ExecutionPayloadError(
                f"field {field_name!r} is not allowed for execution_type "
                f"{execution_type!r}"
            )
        if is_denied_field_name(field_name):
            raise ExecutionPayloadError(
                f"field {field_name!r} is not allowed (sensitive field name)"
            )
        normalized[field_name] = value
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return normalized, payload_hash
