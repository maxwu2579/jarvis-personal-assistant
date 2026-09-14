"""Step Orchestration and Tool Safety（Phase 6C）。

职责：在既有 6A/6B 状态机与 attempt 重试机制之上，实现最小、确定性的
**线性步骤编排** + 工具安全门 + 安全恢复：

- 步骤模板完全来自注册表静态声明（execution_registry.py）——模型文本
  永远只是 handler 的数据输入，不参与任何代码路径解析；无 DAG、无并行、
  无条件图、无动态规划；
- 一个 attempt 按 step_index 顺序执行；前一步成功才能进入下一步；
  步骤失败按工具 retryable 策略交给现有 attempt retry 机制（绝不另起
  第二套 retry 系统）；Execution 终态仍由 6A 状态机裁决；
- 每步写入（step 行 + 对应事件）在同一清晰事务边界内提交；事务内先做
  lease 校验（attempt 条件 UPDATE，token 不匹配 → EXEC_CLAIM_LOST，
  stale Worker 在第一次写入即终止，不写 step/event/终态）；
- 恢复规则：SUCCEEDED 步骤无条件跳过（不重跑、不覆写）；PENDING 可执行；
  FAILED 可按 attempt 重试重新执行（历史由事件流保留）；RUNNING 遇崩溃
  时仅工具显式 replay_safe=True 且非 destructive 才允许重放，否则
  EXEC_STEP_REPLAY_UNSAFE 稳定失败；
- confirmation 绑定 (execution_id, step_id, tool_name, input_hash)：
  确认状态落在 execution_steps 行上（最小可靠方案，不引入单独表）；
  输入 hash 变化 → 旧确认立即失效（EXPIRED），绝不跨 hash 复用；
  无 GRANTED 记录 Worker 绝不执行；本阶段无默认确认、无 HTTP/前端界面，
  测试通过服务层明确注入 actor 确认。
"""

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.database import DIALECT
from app.core.dialect import coerce_utc_for_db
from app.core.sanitize import (
    has_denied_field_name,
    sanitize_error_message,
    sanitize_result_value,
)
from app.models.execution import TERMINAL_STATUSES, Execution, ExecutionStatus
from app.models.execution_attempt import (
    LIVE_ATTEMPT_STATUSES,
    ExecutionAttempt,
)
from app.models.execution_step import ConfirmationStatus, ExecutionStep, StepStatus
from app.services.execution_errors import (
    ConfirmationExpiredError,
    ConfirmationRejectedError,
    ConfirmationRequiredError,
    EventConflictError,
    ExecutionClaimLostError,
    ExecutionError,
    ExecutionStepError,
    InvalidExecutionTransitionError,
    StepConflictError,
    StepInvalidError,
    StepReplayUnsafeError,
    StepSchemaError,
    ToolNotAllowedError,
)
from app.services.execution_events import append_event
from app.services.execution_registry import (
    EXECUTION_TOOLS,
    ExecutionTypeSpec,
    StepSpec,
    ToolSpec,
    get_tool,
    get_type_spec,
    resolve_step_template,
)
from app.services.execution_service import get_execution

# 模板标识符规范（DB 列宽约束之外的第二道防线，fail-closed）
STEP_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,50}$")

# 步骤输入落盘上限：完整敏感输入绝不落盘（input_hash 仍在完整输入上计算）
STEP_INPUT_MAX_CHARS = 2000


def list_execution_steps(db: Session, execution_id: int) -> list[ExecutionStep]:
    """返回 execution 当前已物化的步骤，按模板位置稳定排序。

    这是 Phase 7.2A 的纯只读查询边界：先复用现有 execution 存在性
    校验，再读取 ``execution_steps`` 当前行。它不会物化步骤、写事件、
    改变确认状态或触发工具；尚未产生步骤的 execution 合法返回空列表。
    """
    get_execution(db, execution_id)
    return list(
        db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.execution_id == execution_id)
            .order_by(ExecutionStep.step_index.asc(), ExecutionStep.id.asc())
        ).scalars().all()
    )

# 可确认状态：只有尚未终态且可能执行步骤的 execution 才接受确认动作
_CONFIRMABLE_STATUSES: frozenset[str] = frozenset(
    {
        ExecutionStatus.QUEUED.value,
        ExecutionStatus.CLAIMED.value,
        ExecutionStatus.RUNNING.value,
    }
)


def _to_db_utc(value: datetime) -> datetime:
    """写入数据库前的 UTC 归一化（方言唯一时区接缝，见 core/dialect.py）。"""
    return coerce_utc_for_db(value, DIALECT)


def _now(clock: Clock | None) -> datetime:
    return _to_db_utc((clock or SystemClock()).now())


def _canonical_json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _hash_of(value) -> str:
    """规范化输入/输出指纹（键排序无关；confirmation 的绑定指纹）。"""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _cap_input(value) -> str:
    """步骤输入落盘：规范化序列化 + 截断（完整敏感输入绝不落盘）。"""
    text = _canonical_json(value)
    return text[:STEP_INPUT_MAX_CHARS]


# ---- 租约保护：stale Worker 的第一次写入即终止 ----

def _assert_lease(
    db: Session, execution: Execution, attempt: ExecutionAttempt, now: datetime
) -> None:
    """校验租约仍由本 worker 持有（条件 UPDATE，rowcount 裁决）。

    每次 step 写入边界都调用；token 不匹配/attempt 已终态 → rowcount=0 →
    EXEC_CLAIM_LOST，调用方立即终止，绝不继续写 step、event 或终态。
    不提交——与调用方的状态写入同事务提交。
    """
    result = db.execute(
        update(ExecutionAttempt)
        .where(
            ExecutionAttempt.id == attempt.id,
            ExecutionAttempt.lease_token == attempt.lease_token,
            ExecutionAttempt.status.in_([s.value for s in LIVE_ATTEMPT_STATUSES]),
        )
        .values(updated_at=now)
    )
    if result.rowcount == 0:
        raise ExecutionClaimLostError(execution.id, attempt.id)


# ---- 输入解析 ----

def _resolve_input(execution: Execution, step_spec: StepSpec) -> dict:
    """解析步骤输入：execution 规范化 payload 或模板静态字面量。

    输入必须是非空 dict（handler 契约）；静态来源保证每次 attempt 的
    输入一致（hash 绑定因此稳定）。
    """
    if step_spec.input_source == "literal":
        value = step_spec.input_literal if step_spec.input_literal is not None else {}
    elif step_spec.input_source == "payload":
        value = json.loads(execution.payload)
    else:
        raise StepInvalidError(
            f"step {step_spec.step_key!r}: unknown input_source "
            f"{step_spec.input_source!r}"
        )
    if not isinstance(value, dict):
        raise StepInvalidError(
            f"step {step_spec.step_key!r}: input must be a JSON object"
        )
    return value


def _step_spec_for(
    db: Session, execution: Execution, step_key: str
) -> StepSpec:
    """按 step_key 查模板中的步骤声明；不存在 → EXEC_STEP_INVALID。"""
    spec = get_type_spec(execution.execution_type)
    template = resolve_step_template(spec)
    step_spec = next((s for s in template if s.step_key == step_key), None)
    if step_spec is None:
        raise StepInvalidError(
            f"Execution {execution.id} has no step {step_key!r}"
        )
    return step_spec


# ---- 步骤物化（惰性：首次接触或首次确认时创建） ----

def _materialize_step(
    db: Session,
    execution: Execution,
    step_spec: StepSpec,
    index: int,
    tool: ToolSpec,
    *,
    attempt_id: int | None = None,
    clock: Clock | None = None,
    commit: bool = True,
) -> ExecutionStep:
    """创建步骤行（PENDING）。已存在 → 返回既有行（恢复语义）。

    (execution_id, step_index) / (execution_id, step_key) 由数据库唯一
    约束裁决并发；冲突（同索引不同键 = 模板漂移）→ EXEC_STEP_CONFLICT。
    input_hash 在完整输入上计算（确认绑定指纹），input_json 截断存储。
    """
    now = _now(clock)
    existing = db.execute(
        select(ExecutionStep).where(
            ExecutionStep.execution_id == execution.id,
            ExecutionStep.step_index == index,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    input_value = _resolve_input(execution, step_spec)
    if has_denied_field_name(input_value):
        # 敏感字段 fail-closed：在任何落盘之前拒绝——不创建步骤行、
        # 不写入 input_json，敏感内容绝不进入数据库
        raise StepSchemaError(
            f"step {step_spec.step_key!r} input contains a sensitive field name"
        )
    step = ExecutionStep(
        execution_id=execution.id,
        attempt_id=attempt_id,
        step_key=step_spec.step_key,
        step_index=index,
        tool_name=tool.name,
        status=StepStatus.PENDING.value,
        input_json=_cap_input(input_value),
        input_hash=_hash_of(input_value),
        requires_confirmation=tool.requires_confirmation,
        confirmation_status=(
            ConfirmationStatus.PENDING.value if tool.requires_confirmation else None
        ),
        created_at=now,
        updated_at=now,
    )
    db.add(step)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(ExecutionStep).where(
                ExecutionStep.execution_id == execution.id,
                ExecutionStep.step_index == index,
            )
        ).scalar_one_or_none()
        if existing is None or existing.step_key != step_spec.step_key:
            raise StepConflictError(execution.id, index) from None
        return existing
    if commit:
        db.refresh(step)
    return step


def _get_or_create_step(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    step_spec: StepSpec,
    index: int,
    tool: ToolSpec,
    clock: Clock | None,
) -> ExecutionStep:
    """Worker 路径的步骤物化：先校验租约，再物化（同事务提交）。"""
    _assert_lease(db, execution, attempt, _now(clock))
    return _materialize_step(db, execution, step_spec, index, tool,
                             attempt_id=attempt.id, clock=clock)


# ---- 步骤写入边界（均含 lease 校验 + 条件 UPDATE + 同事务事件） ----

def _start_step(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    step: ExecutionStep,
    clock: Clock | None,
) -> None:
    """PENDING/FAILED/RUNNING → RUNNING + STEP_STARTED（同事务）。"""
    now = _now(clock)
    _assert_lease(db, execution, attempt, now)
    result = db.execute(
        update(ExecutionStep)
        .where(
            ExecutionStep.id == step.id,
            ExecutionStep.status.in_(
                [StepStatus.PENDING.value, StepStatus.RUNNING.value, StepStatus.FAILED.value]
            ),
        )
        .values(
            status=StepStatus.RUNNING.value,
            attempt_id=attempt.id,
            started_at=step.started_at or now,  # 首次启动时间保留（历史在事件流）
            error_code=None,
            error_message=None,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        raise StepConflictError(execution.id, step.step_index)
    append_event(
        db,
        execution_id=execution.id,
        event_type="STEP_STARTED",
        attempt_id=attempt.id,
        step_id=step.id,
        actor_type="worker",
        actor_id=attempt.worker_id,
        correlation_id=execution.correlation_id,
        payload=_step_event_payload(step, attempt, input_hash=step.input_hash),
        clock=clock,
    )
    db.commit()
    db.refresh(step)


def _fail_step(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    step: ExecutionStep,
    error_code: str,
    error_message: str,
    clock: Clock | None,
    *,
    extra_events: tuple[tuple[str, dict], ...] = (),
) -> None:
    """PENDING/RUNNING → FAILED + STEP_FAILED（同事务，error 已脱敏）。

    extra_events：gate 阻塞时的补充事件（如 CONFIRMATION_REQUIRED），
    先于 STEP_FAILED 写入。
    """
    now = _now(clock)
    _assert_lease(db, execution, attempt, now)
    result = db.execute(
        update(ExecutionStep)
        .where(
            ExecutionStep.id == step.id,
            ExecutionStep.status.in_(
                [StepStatus.PENDING.value, StepStatus.RUNNING.value]
            ),
        )
        .values(
            status=StepStatus.FAILED.value,
            error_code=error_code,
            error_message=sanitize_error_message(error_message),
            finished_at=now,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        raise StepConflictError(execution.id, step.step_index)
    for event_type, payload in extra_events:
        append_event(
            db,
            execution_id=execution.id,
            event_type=event_type,
            attempt_id=attempt.id,
            step_id=step.id,
            actor_type="worker",
            actor_id=attempt.worker_id,
            correlation_id=execution.correlation_id,
            payload=payload,
            clock=clock,
        )
    append_event(
        db,
        execution_id=execution.id,
        event_type="STEP_FAILED",
        attempt_id=attempt.id,
        step_id=step.id,
        actor_type="worker",
        actor_id=attempt.worker_id,
        correlation_id=execution.correlation_id,
        payload=_step_event_payload(
            step, attempt,
            error_code=error_code,
            error_message=sanitize_error_message(error_message),
        ),
        clock=clock,
    )
    db.commit()
    db.refresh(step)


def _finish_step(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    step: ExecutionStep,
    output,
    clock: Clock | None,
) -> None:
    """RUNNING → SUCCEEDED + output_json（已脱敏）+ STEP_SUCCEEDED（同事务）。

    竞争完成只有一个事务获胜：条件 UPDATE（status=RUNNING）+ lease 校验
    双闸门——并发完成/超时/回收路径无法双写。
    """
    now = _now(clock)
    _assert_lease(db, execution, attempt, now)
    output_json = _canonical_json(output)
    result = db.execute(
        update(ExecutionStep)
        .where(
            ExecutionStep.id == step.id,
            ExecutionStep.status == StepStatus.RUNNING.value,
        )
        .values(
            status=StepStatus.SUCCEEDED.value,
            output_json=output_json,
            error_code=None,
            error_message=None,
            finished_at=now,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        raise StepConflictError(execution.id, step.step_index)
    append_event(
        db,
        execution_id=execution.id,
        event_type="STEP_SUCCEEDED",
        attempt_id=attempt.id,
        step_id=step.id,
        actor_type="worker",
        actor_id=attempt.worker_id,
        correlation_id=execution.correlation_id,
        payload=_step_event_payload(
            step, attempt, output_hash=_hash_of(output)
        ),
        clock=clock,
    )
    db.commit()
    db.refresh(step)


def _sync_input_hash(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    step: ExecutionStep,
    input_hash: str,
    input_value: dict,
    clock: Clock | None,
) -> None:
    """模板/注册表漂移：输入 hash 与物化时不同 → 同步；旧确认立即失效。

    已 GRANTED/REJECTED 的确认是针对旧输入的决策，一律 EXPIRED（须重新
    决策），绝不跨输入 hash 复用。
    """
    now = _now(clock)
    _assert_lease(db, execution, attempt, now)
    values: dict = {
        "input_hash": input_hash,
        "input_json": _cap_input(input_value),
        "updated_at": now,
    }
    if step.requires_confirmation and step.confirmation_status in (
        ConfirmationStatus.GRANTED.value,
        ConfirmationStatus.REJECTED.value,
    ):
        values["confirmation_status"] = ConfirmationStatus.EXPIRED.value
    db.execute(
        update(ExecutionStep).where(ExecutionStep.id == step.id).values(**values)
    )
    # 规格固定 17 种事件类型（无 CONFIRMATION_EXPIRED）：EXPIRED 状态转换
    # 由后续确认门的 STEP_FAILED（EXEC_CONFIRMATION_EXPIRED）记录审计。
    db.commit()
    db.refresh(step)


# ---- 确认门 ----

def _check_confirmation_gate(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    step: ExecutionStep,
    clock: Clock | None,
) -> None:
    """确认门：无有效 GRANTED 记录 Worker 不得执行（不调用 handler）。

    - GRANTED：放行（input_hash 已在上游校验一致——绑定未失效）；
    - REJECTED：稳定失败（EXEC_CONFIRMATION_REJECTED，不重试）；
    - EXPIRED：需重新确认（EXEC_CONFIRMATION_EXPIRED，可重试）；
    - PENDING/无记录：等待确认（EXEC_CONFIRMATION_REQUIRED，可重试，
      受 max_attempts 上限约束——绝不无限等待）。
    """
    status = (
        ConfirmationStatus(step.confirmation_status)
        if step.confirmation_status
        else ConfirmationStatus.PENDING
    )
    if status is ConfirmationStatus.GRANTED:
        return
    if status is ConfirmationStatus.REJECTED:
        _fail_step(
            db, execution, attempt, step,
            "EXEC_CONFIRMATION_REJECTED",
            f"confirmation for step {step.step_key!r} was rejected",
            clock,
        )
        raise ConfirmationRejectedError(step.step_key, step.tool_name)
    if status is ConfirmationStatus.EXPIRED:
        _fail_step(
            db, execution, attempt, step,
            "EXEC_CONFIRMATION_EXPIRED",
            f"confirmation for step {step.step_key!r} has expired; "
            "re-confirmation required",
            clock,
        )
        raise ConfirmationExpiredError(step.step_key, step.tool_name)
    # PENDING
    _fail_step(
        db, execution, attempt, step,
        "EXEC_CONFIRMATION_REQUIRED",
        f"step {step.step_key!r} (tool {step.tool_name!r}) requires "
        "confirmation before execution",
        clock,
        extra_events=(
            (
                "CONFIRMATION_REQUIRED",
                _step_event_payload(
                    step, attempt, input_hash=step.input_hash
                ),
            ),
        ),
    )
    raise ConfirmationRequiredError(step.step_key, step.tool_name)


# ---- 主编排 ----

def run_execution_steps(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    *,
    clock: Clock | None = None,
):
    """按模板顺序执行一个 execution 的步骤（单 attempt 内）。

    返回最后成功步骤的脱敏输出（单步 execution 与 6B 的 result_text
    语义一致）。步骤失败抛出 ExecutionStepError（携带稳定 EXEC_ 错误码
    与 retryable 标志），由 Worker 按既有 attempt 重试/失败路径裁决。

    终态 execution 拒绝编排（防御）；模板为空/标识符非法 → 稳定失败。
    """
    if execution.status in {s.value for s in TERMINAL_STATUSES}:
        raise StepInvalidError(
            f"Execution {execution.id} is terminal; steps cannot run"
        )
    _assert_lease(db, execution, attempt, _now(clock))
    spec = get_type_spec(execution.execution_type)
    template = resolve_step_template(spec)
    if not template:
        raise StepInvalidError(
            f"execution_type {execution.execution_type!r} declares no steps"
        )

    last_output = None
    for index, step_spec in enumerate(template):
        if not STEP_KEY_RE.match(step_spec.step_key):
            raise StepInvalidError(f"invalid step_key {step_spec.step_key!r}")
        if not TOOL_NAME_RE.match(step_spec.tool_name):
            raise StepInvalidError(f"invalid tool_name {step_spec.tool_name!r}")
        tool = get_tool(step_spec.tool_name, legacy_fallback=(spec.steps is None))
        if not tool.enabled:
            raise ToolNotAllowedError(step_spec.tool_name)

        step = _get_or_create_step(db, execution, attempt, step_spec, index, tool, clock)

        # ---- 恢复规则 ----
        if step.status == StepStatus.SUCCEEDED.value:
            # 已成功完成的安全步骤：无条件跳过，绝不重跑、绝不覆写
            last_output = (
                json.loads(step.output_json) if step.output_json else None
            )
            continue
        if step.status == StepStatus.RUNNING.value:
            # 上个 attempt 崩溃残留：仅显式 replay_safe 且非 destructive
            # 才允许重放；否则稳定失败（绝不自 动重放）
            if tool.destructive or not tool.replay_safe:
                _fail_step(
                    db, execution, attempt, step,
                    "EXEC_STEP_REPLAY_UNSAFE",
                    "step was interrupted by a previous attempt and its "
                    "tool is not replay-safe",
                    clock,
                )
                raise StepReplayUnsafeError(step.step_key, step.tool_name)

        # ---- 输入解析 + 敏感字段 fail-closed + hash 绑定 ----
        input_value = _resolve_input(execution, step_spec)
        if has_denied_field_name(input_value):
            _fail_step(
                db, execution, attempt, step,
                "EXEC_SCHEMA_INVALID",
                f"step {step.step_key!r} input contains a sensitive "
                "field name",
                clock,
            )
            raise StepSchemaError(
                f"step {step.step_key!r} input contains a sensitive field name"
            )
        input_hash = _hash_of(input_value)
        if step.input_hash != input_hash:
            # 模板/注册表漂移：同步绑定指纹；旧确认（若存在）立即失效
            _sync_input_hash(
                db, execution, attempt, step, input_hash, input_value, clock
            )

        # ---- 输入 schema（unknown fields 默认拒绝，strict） ----
        if tool.input_schema is not None:
            try:
                tool.input_schema.model_validate(input_value, strict=True)
            except Exception:  # noqa: BLE001 - ValidationError 及意外一律拒绝
                _fail_step(
                    db, execution, attempt, step,
                    "EXEC_SCHEMA_INVALID",
                    f"step {step.step_key!r} input failed schema validation",
                    clock,
                )
                raise StepSchemaError(
                    f"step {step.step_key!r} input failed schema validation"
                ) from None

        # ---- 确认门 ----
        requires_confirmation = tool.requires_confirmation or step.requires_confirmation
        if requires_confirmation:
            if not step.requires_confirmation:
                # 工具策略升级（注册表漂移）：同步行内镜像后过门
                now = _now(clock)
                _assert_lease(db, execution, attempt, now)
                db.execute(
                    update(ExecutionStep)
                    .where(ExecutionStep.id == step.id)
                    .values(requires_confirmation=True, updated_at=now)
                )
                db.commit()
                db.refresh(step)
            _check_confirmation_gate(db, execution, attempt, step, clock)

        # ---- 执行（handler 是注册表静态白名单函数） ----
        _start_step(db, execution, attempt, step, clock)
        try:
            if tool.handler is None:
                raise ExecutionError(
                    "EXEC_HANDLER_ERROR",
                    f"No handler registered for tool {tool.name!r}",
                )
            raw_output = tool.handler(input_value)
        except Exception as exc:  # noqa: BLE001 - handler 失败按步骤失败处理
            code = getattr(exc, "code", None) or "EXEC_HANDLER_ERROR"
            message = sanitize_error_message(str(exc))
            _fail_step(db, execution, attempt, step, code, message, clock)
            raise ExecutionStepError(
                code, message, retryable=bool(getattr(exc, "retryable", tool.retryable)),
                step_key=step.step_key,
            ) from exc

        # ---- 输出 schema（unknown fields 默认拒绝，strict） ----
        if tool.output_schema is not None:
            try:
                tool.output_schema.model_validate(raw_output, strict=True)
            except Exception:  # noqa: BLE001 - 校验失败一律拒绝
                _fail_step(
                    db, execution, attempt, step,
                    "EXEC_SCHEMA_INVALID",
                    f"step {step.step_key!r} output failed schema validation",
                    clock,
                )
                raise StepSchemaError(
                    f"step {step.step_key!r} output failed schema validation"
                ) from None

        # ---- 落盘（sanitization policy） ----
        output = (
            sanitize_result_value(raw_output)
            if tool.sanitize_result
            else raw_output
        )
        _finish_step(db, execution, attempt, step, output, clock)
        last_output = output
    return last_output


# ---- Confirmation 服务（service/domain 层；本阶段无 HTTP/前端） ----

def _resolve_step_for_confirmation(
    db: Session,
    execution: Execution,
    step_key: str,
    clock: Clock | None,
    commit: bool = True,
) -> ExecutionStep:
    """定位步骤行；不存在则从模板物化（允许在首次 attempt 前确认）。"""
    step = db.execute(
        select(ExecutionStep).where(
            ExecutionStep.execution_id == execution.id,
            ExecutionStep.step_key == step_key,
        )
    ).scalar_one_or_none()
    if step is not None:
        return step
    step_spec = _step_spec_for(db, execution, step_key)
    spec = get_type_spec(execution.execution_type)
    tool = get_tool(step_spec.tool_name, legacy_fallback=(spec.steps is None))
    index = resolve_step_template(spec).index(step_spec)
    return _materialize_step(db, execution, step_spec, index, tool, clock=clock, commit=commit)


def _confirm(
    db: Session,
    execution_id: int,
    step_key: str,
    *,
    actor_type: str,
    actor_id: str,
    status: ConfirmationStatus,
    event_type: str,
    clock: Clock | None,
    commit: bool = True,
) -> ExecutionStep:
    """grant/reject 共用骨架：状态 + actor 落盘 + 事件（同事务）。"""
    if event_type not in ("CONFIRMATION_GRANTED", "CONFIRMATION_REJECTED"):
        raise ValueError(f"invalid confirmation event type {event_type!r}")
    execution = get_execution(db, execution_id)
    if execution.status not in _CONFIRMABLE_STATUSES:
        raise InvalidExecutionTransitionError(
            execution_id, execution.status, status.value
        )
    now = _now(clock)
    step = _resolve_step_for_confirmation(db, execution, step_key, clock, commit=commit)
    if step.status == StepStatus.SUCCEEDED.value:
        raise StepInvalidError(
            f"step {step_key!r} already succeeded; confirmation is not needed"
        )
    # 绑定当前输入 hash：输入静态（payload 不可变 + 模板静态）；若注册表
    # 漂移则同步指纹后再确认——确认永远绑定实际将执行的输入
    step_spec = _step_spec_for(db, execution, step_key)
    input_value = _resolve_input(execution, step_spec)
    if has_denied_field_name(input_value):
        # 敏感字段 fail-closed：确认动作绝不绑定敏感输入（且不落盘）
        raise StepSchemaError(
            f"step {step_key!r} input contains a sensitive field name"
        )
    input_hash = _hash_of(input_value)
    values: dict = {
        "confirmation_status": status.value,
        "confirmation_actor_type": actor_type,
        "confirmation_actor_id": actor_id,
        "confirmed_at": now,
        "updated_at": now,
    }
    if step.input_hash != input_hash:
        values["input_hash"] = input_hash
        values["input_json"] = _cap_input(input_value)
    # 6E 状态门（CAS）：只有 PENDING/EXPIRED 可被决策（EXPIRED = 旧确认
    # 失效后重新决策）。GRANTED/REJECTED 是终态决策——条件 UPDATE rowcount
    # 裁决并发（确认与 Worker/双确认竞争绝不双赢）；rowcount=0 时重查：
    # 同 actor 同状态 → 幂等返回；否则稳定 409 EXEC_STEP_CONFLICT（不可翻转）。
    result = db.execute(
        update(ExecutionStep)
        .where(
            ExecutionStep.id == step.id,
            ExecutionStep.confirmation_status.in_(
                [
                    ConfirmationStatus.PENDING.value,
                    ConfirmationStatus.EXPIRED.value,
                ]
            ),
        )
        .values(**values)
    )
    if result.rowcount == 0:
        db.rollback()
        db.refresh(step)
        current = (
            ConfirmationStatus(step.confirmation_status)
            if step.confirmation_status
            else ConfirmationStatus.PENDING
        )
        if current.value == status.value and step.confirmation_actor_id == actor_id:
            return step  # 同 actor 同状态：幂等（并发双击）
        raise StepConflictError(execution.id, step.step_index)
    append_event(
        db,
        execution_id=execution_id,
        event_type=event_type,
        step_id=step.id,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=execution.correlation_id,
        payload={
            "step": step.step_key,
            "tool_name": step.tool_name,
            "input_hash": input_hash,
            "actor_type": actor_type,
            "actor_id": actor_id,
        },
        clock=clock,
    )
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise EventConflictError(execution_id, event_type) from exc
    if commit:
        db.refresh(step)
    return step


def grant_confirmation(
    db: Session,
    execution_id: int,
    step_key: str,
    *,
    actor_type: str,
    actor_id: str,
    clock: Clock | None = None,
    commit: bool = True,
) -> ExecutionStep:
    """授予确认（显式 actor；绝不伪造默认用户确认）。

    确认绑定 (execution_id, step_id, tool_name, input_hash)——绑定指纹
    存于步骤行；输入变化后旧确认立即失效（EXPIRED）。
    """
    return _confirm(
        db, execution_id, step_key,
        actor_type=actor_type, actor_id=actor_id,
        status=ConfirmationStatus.GRANTED,
        event_type="CONFIRMATION_GRANTED",
        clock=clock, commit=commit,
    )


def reject_confirmation(
    db: Session,
    execution_id: int,
    step_key: str,
    *,
    actor_type: str,
    actor_id: str,
    clock: Clock | None = None,
) -> ExecutionStep:
    """拒绝确认：步骤稳定失败（EXEC_CONFIRMATION_REJECTED，不重试）。"""
    return _confirm(
        db, execution_id, step_key,
        actor_type=actor_type, actor_id=actor_id,
        status=ConfirmationStatus.REJECTED,
        event_type="CONFIRMATION_REJECTED",
        clock=clock,
    )


def _step_event_payload(step: ExecutionStep, attempt: ExecutionAttempt, **extra):
    # 事件 payload 键是系统声明的元数据，但 sanitize deny 启发式把任何
    # *_key / *_token 后缀的键名视为敏感字段并改写为 <redacted>——因此
    # 用 "step" 而非 "step_key"（值域绝不含敏感内容，error_message 已预脱敏）。
    payload: dict = {
        "step": step.step_key,
        "step_index": step.step_index,
        "tool_name": step.tool_name,
        "attempt_number": attempt.attempt_number,
    }
    payload.update(extra)
    return payload
