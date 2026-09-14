"""任务建议（Task Proposal）的请求 / 响应 schema。

安全设计：
- action 白名单：仅 Literal["create_task_draft"]，任何其他值直接 422/502；
- arguments 直接复用 TaskCreate —— 标题空白、长度、due_at 时区规则与任务创建
  完全同一套逻辑，不存在两套规则；
- explanation 是描述性文本，只向用户解释，绝不作为执行指令；
- 所有输入模型 extra="forbid"。
"""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas.chat import MessageOut, UsageOut
from app.schemas.common import serialize_utc, strip_nonempty
from app.schemas.task import TaskCreate, TaskOut

PROPOSAL_ACTION = Literal["create_task_draft"]

MAX_EXPLANATION = 500


class TaskProposal(BaseModel):
    """LLM 输出的结构化建议（Structured Output 目标结构）。"""

    model_config = ConfigDict(extra="forbid")

    action: PROPOSAL_ACTION = "create_task_draft"
    arguments: TaskCreate
    explanation: str = Field(max_length=MAX_EXPLANATION)

    @field_validator("explanation")
    @classmethod
    def _strip_explanation(cls, value: str) -> str:
        return strip_nonempty(value, "explanation")


class TaskProposalRequest(BaseModel):
    """POST /task-proposals 请求：用户自然语言 + IANA 时区。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=8000)
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        return strip_nonempty(value, "content")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        cleaned = value.strip()
        try:
            ZoneInfo(cleaned)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"invalid IANA timezone {cleaned!r}") from exc
        return cleaned


class TaskProposalOut(BaseModel):
    """审计记录输出：含规范化后的 arguments 与审计错误信息（REJECTED 时）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    user_message_id: int
    assistant_message_id: int | None
    task_id: int | None
    action: str | None
    arguments: TaskCreate | None
    explanation: str | None
    status: str
    error_code: str | None
    error_detail: str | None
    created_at: datetime

    @field_validator("arguments", mode="before")
    @classmethod
    def _parse_arguments(cls, value):
        if isinstance(value, str):
            return json.loads(value)
        return value

    @field_serializer("created_at")
    def _ser_utc(self, value: datetime) -> str:
        return serialize_utc(value)


class TaskProposalExchangeOut(BaseModel):
    """发送建议的完整响应。"""

    user_message: MessageOut
    assistant_message: MessageOut
    proposal: TaskProposalOut
    created_task: TaskOut
    usage: UsageOut


class TaskProposalWithTaskOut(BaseModel):
    """读取接口：proposal 与其任务（REJECTED 时 task 为 null）。"""

    proposal: TaskProposalOut
    task: TaskOut | None
