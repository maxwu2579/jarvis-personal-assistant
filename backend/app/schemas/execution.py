"""Execution API schema（Phase 6E）。

输出约定：
- 所有 datetime 经 serialize_utc（UTC ISO 8601，'Z' 结尾，naive 读回视为 UTC）；
- ExecutionOut 绝不包含 payload 正文（可含敏感字段，虽已白名单规范化）、
  lease_token/lease_owner/lease_expires_at（worker 租约内部细节）；
- 输入 schema extra="forbid"：body 带未知字段（含 idempotency_key、
  confirmation_status 等）→ 422，杜绝绕过 Header/服务层的捷径。
"""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.schemas.common import serialize_utc, strip_nonempty


class ExecutionOut(BaseModel):
    """Execution 公开视图（安全 schema，不含 payload 原文与租约细节）。

    时间字段类型为 datetime（from_attributes 校验接受 ORM 值），输出经
    field_serializer 序列化为 UTC ISO 8601（'Z' 结尾）——与 ReminderOut
    同模式。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: str
    execution_type: str
    status: str
    idempotency_key: str
    normalized_payload_hash: str
    run_at: datetime | None = None
    retry_after: datetime | None = None
    correlation_id: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    attempt_count: int
    max_attempts: int
    timeout_seconds: int | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    retry_of_id: int | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "run_at",
        "retry_after",
        "queued_at",
        "started_at",
        "finished_at",
        "cancel_requested_at",
        "created_at",
        "updated_at",
    )
    def _serialize_dt(self, value):
        return serialize_utc(value)


class ExecutionDocumentTarget(BaseModel):
    """documents.reindex 的公开业务目标（显式 allowlist 投影）。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["document"] = "document"
    document_id: int


class ExecutionCalendarTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["calendar_event"] = "calendar_event"
    title: str = Field(max_length=200)
    start_at: datetime
    end_at: datetime
    location: str | None = Field(default=None, max_length=300)

    @field_serializer("start_at", "end_at")
    def _serialize_times(self, value: datetime):
        return serialize_utc(value)


class ExecutionCalendarResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_event_id: str = Field(max_length=500)
    title: str = Field(max_length=200)
    start_at: datetime
    end_at: datetime
    location: str | None = Field(default=None, max_length=300)

    @field_serializer("start_at", "end_at")
    def _serialize_times(self, value: datetime):
        return serialize_utc(value)


class ExecutionDetailOut(ExecutionOut):
    """Execution 单项详情；仅增加安全、类型化的业务目标。"""

    target: ExecutionDocumentTarget | ExecutionCalendarTarget | None = None
    result: ExecutionCalendarResult | None = None


class ExecutionEventOut(BaseModel):
    """审计事件视图（sequence_number 升序输出；payload 已脱敏落库）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    execution_id: int
    sequence_number: int
    event_type: str
    actor_type: str
    actor_id: str
    attempt_id: int | None = None
    step_id: int | None = None
    correlation_id: str | None = None
    payload: dict | None = None
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_dt(self, value):
        return serialize_utc(value)

    @field_validator("payload", mode="before")
    @classmethod
    def _parse_payload(cls, value):
        """DB 存储为 JSON 文本；解析失败（防御历史数据）返回 None。"""
        if value is None or isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None


class ExecutionStepOut(BaseModel):
    """步骤摘要视图（确认/拒绝响应；不含 input/output 原文）。

    input_json/output_json 是步骤的工作数据（输入可为敏感内容截断预览、
    输出可为任意脱敏结果），默认响应不返回——需要时经 events API 审计。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    execution_id: int
    step_key: str
    step_index: int
    tool_name: str
    status: str
    requires_confirmation: bool
    confirmation_status: str | None = None
    confirmation_actor_type: str | None = None
    confirmation_actor_id: str | None = None
    confirmed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "confirmed_at", "started_at", "finished_at", "created_at", "updated_at"
    )
    def _serialize_dt(self, value):
        return serialize_utc(value)


class ExecutionCreate(BaseModel):
    """创建 Execution 请求体。

    Idempotency-Key 只经 HTTP Header（规格五）：extra="forbid" 拒绝任何
    body 字段（含 idempotency_key）与旁路替代。
    """

    model_config = ConfigDict(extra="forbid")

    execution_type: str
    payload: dict = Field(default_factory=dict)
    run_at: str | None = None

    @field_validator("run_at")
    @classmethod
    def _parse_run_at(cls, value):
        """run_at 以字符串传入：经 datetime.fromisoformat 解析为 aware。

        naive 输入在此不拒绝（保持 6D 的 service 层 EXEC_RUN_AT_NAIVE
        稳定错误码路径——create_execution._validate_run_at 裁决）。
        """
        from datetime import datetime

        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                "run_at must be an ISO 8601 datetime, e.g. "
                "2026-08-11T04:00:00+00:00"
            ) from None
        return parsed


class ExecutionCreateOut(BaseModel):
    """创建响应：201（首次）/ 200（重放，replayed=True）。"""

    execution: ExecutionOut
    replayed: bool


class ConfirmStepRequest(BaseModel):
    """确认/拒绝请求体。

    规格九：显式 actor 必填（无默认 actor）；body 不得直接指定
    confirmation_status——extra="forbid" 下任何多余字段（含
    confirmation_status）→ 422，确认状态只由 confirm/reject 端点语义决定。
    """

    model_config = ConfigDict(extra="forbid")

    actor: str
    actor_type: str = "user"

    @field_validator("actor")
    @classmethod
    def _strip_actor(cls, value):
        return strip_nonempty(value, "actor")
