"""Phase 8A Natural Language -> validated, read-only Action Proposal."""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.llm.action_prompts import (
    build_action_proposal_system_prompt,
    build_action_proposal_user_prompt,
)
from app.llm.base import LLMProvider, LLMResponse
from app.llm.messages import build_provider_messages
from app.models.conversation import Message
from app.schemas.action_proposal import (
    ActionProposalCandidate,
    ActionCalendarEventPreview,
    ActionProposalOut,
    ActionReminderPreview,
    ActionTaskPreview,
)
from app.services.action_datetime import ActionTimeError, parse_user_datetime, parse_user_datetime_range
from app.services.chat_service import get_conversation


UNSUPPORTED_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"删除(?:所有|全部)?文档",
        r"(?:删除|删掉|移除).{0,12}任务",
        r"任务.{0,8}(?:删了|删除|删掉|移除)",
        r"(?:修改|更新|改).{0,12}任务|任务.{0,8}(?:修改|更新|改成)",
        r"(?:删除|取消).{0,12}提醒",
        r"(?:发|发送).{0,6}(?:邮件|email)",
        r"google\s*calendar",
        r"(?:每天|每周|每月|重复|循环).{0,20}(?:日历|事件|会议)",
        r"(?:每天|每周|每月|重复|循环).{0,20}(?:安排|晨会|活动)",
        r"(?:邀请|参会人|参与者|attendee)",
        r"全天|整天",
        r"忽略.{0,20}规则.{0,30}(?:创建|执行|调用)",
        r"不需要确认.{0,20}(?:创建|执行)|(?:创建|执行).{0,20}不需要确认",
        r"重新索引|documents\.reindex|system\.ping",
        r"修改.{0,8}数据库|运行.{0,6}命令|执行.{0,6}命令",
        r"直接调用.{0,8}工具|执行任意函数|调用任意函数",
        r"忽略.{0,12}规则.{0,20}(?:执行|调用)",
        r"不要确认.{0,12}(?:执行|调用)",
    )
)
EXECUTION_CLAIM_PATTERN = re.compile(r"已(?:经)?(?:创建|执行|设置|保存|入队|确认)")


def _usage(response: LLMResponse | None) -> tuple[int | None, int | None, int | None]:
    if response is None:
        return None, None, None
    return response.prompt_tokens, response.completion_tokens, response.latency_ms


def _invalid() -> ActionProposalOut:
    return ActionProposalOut(
        status="INVALID",
        action=None,
        task=None,
        reminder=None,
        missing_fields=[],
        clarification_question=None,
        explanation="模型输出未通过安全结构校验，未创建或执行任何内容。",
    )


def _unsupported() -> ActionProposalOut:
    return ActionProposalOut(
        status="UNSUPPORTED",
        action=None,
        task=None,
        reminder=None,
        missing_fields=[],
        clarification_question=None,
        explanation="当前阶段不支持该操作；未创建、修改或执行任何内容。",
    )


def _is_grounded(phrase: str | None, content: str) -> bool:
    if phrase is None:
        return True
    compact_phrase = re.sub(r"\s+", "", phrase).lower()
    compact_content = re.sub(r"\s+", "", content).lower()
    return compact_phrase in compact_content


def _safe_explanation(candidate: ActionProposalCandidate) -> str:
    if EXECUTION_CLAIM_PATTERN.search(candidate.explanation):
        if candidate.intent == "create_calendar_event":
            return "我理解你希望预览一个 Outlook 日历事件；目前尚未创建任何内容。"
        if candidate.intent == "create_task_with_reminder":
            return "我理解你希望预览一个任务与提醒提案；目前尚未创建任何内容。"
        return "我理解你希望预览一个任务提案；目前尚未创建任何内容。"
    return candidate.explanation


def _clarification_question(missing: list[str], errors: list[ActionTimeError]) -> str:
    if errors:
        return errors[0].public_message
    if "task.title" in missing:
        return "你希望创建什么任务？"
    if "reminder.remind_at" in missing:
        return "你希望什么时候提醒？"
    if "task.due_at" in missing:
        return "任务的具体截止时间是几点？"
    if "calendar_event.title" in missing:
        return "日历事件的标题是什么？"
    if "calendar_event.start_at" in missing or "calendar_event.end_at" in missing:
        return "请提供明确的开始时间和结束时间。"
    return "请补充缺少的信息。"


def _build_validated(
    candidate: ActionProposalCandidate,
    *,
    content: str,
    timezone: str,
    now_utc: datetime,
) -> ActionProposalOut:
    if candidate.intent == "unsupported":
        return _unsupported()

    if not _is_grounded(candidate.task_due_text, content) or not _is_grounded(
        candidate.reminder_time_text, content
    ) or not _is_grounded(candidate.calendar_start_text, content) or not _is_grounded(
        candidate.calendar_end_text, content
    ) or not _is_grounded(candidate.calendar_location, content):
        return _invalid()

    missing: list[str] = []
    time_errors: list[ActionTimeError] = []
    due_at = None
    remind_at = None
    calendar_start = None
    calendar_end = None

    if candidate.intent == "create_calendar_event":
        if not candidate.calendar_title:
            missing.append("calendar_event.title")
        if candidate.calendar_start_text is None:
            missing.append("calendar_event.start_at")
        if candidate.calendar_end_text is None:
            missing.append("calendar_event.end_at")
        if candidate.calendar_start_text is not None and candidate.calendar_end_text is not None:
            try:
                calendar_start, calendar_end = parse_user_datetime_range(
                    candidate.calendar_start_text,
                    candidate.calendar_end_text,
                    user_timezone=timezone,
                    now_utc=now_utc,
                )
            except ActionTimeError as exc:
                missing.extend(["calendar_event.start_at", "calendar_event.end_at"])
                time_errors.append(exc)
        event = ActionCalendarEventPreview(
            title=candidate.calendar_title,
            start_at=calendar_start,
            end_at=calendar_end,
            location=candidate.calendar_location,
        )
        if missing:
            return ActionProposalOut(
                status="NEEDS_CLARIFICATION", action=candidate.intent,
                task=None, reminder=None, calendar_event=event,
                missing_fields=list(dict.fromkeys(missing)),
                clarification_question=_clarification_question(missing, time_errors),
                explanation=_safe_explanation(candidate),
            )
        return ActionProposalOut(
            status="READY", action=candidate.intent, task=None, reminder=None,
            calendar_event=event, missing_fields=[], clarification_question=None,
            explanation=_safe_explanation(candidate),
        )

    if not candidate.task_title:
        missing.append("task.title")

    if candidate.task_due_text is not None:
        try:
            due_at = parse_user_datetime(
                candidate.task_due_text,
                user_timezone=timezone,
                now_utc=now_utc,
            )
        except ActionTimeError as exc:
            missing.append("task.due_at")
            time_errors.append(exc)

    if candidate.intent == "create_task_with_reminder":
        if candidate.reminder_time_text is None:
            missing.append("reminder.remind_at")
        else:
            try:
                remind_at = parse_user_datetime(
                    candidate.reminder_time_text,
                    user_timezone=timezone,
                    now_utc=now_utc,
                )
            except ActionTimeError as exc:
                missing.append("reminder.remind_at")
                time_errors.append(exc)

    task = ActionTaskPreview(title=candidate.task_title, due_at=due_at)
    reminder = (
        ActionReminderPreview(remind_at=remind_at)
        if candidate.intent == "create_task_with_reminder"
        else None
    )
    if missing:
        return ActionProposalOut(
            status="NEEDS_CLARIFICATION",
            action=candidate.intent,
            task=task,
            reminder=reminder,
            missing_fields=list(dict.fromkeys(missing)),
            clarification_question=_clarification_question(missing, time_errors),
            explanation=_safe_explanation(candidate),
        )

    return ActionProposalOut(
        status="READY",
        action=candidate.intent,
        task=task,
        reminder=reminder,
        missing_fields=[],
        clarification_question=None,
        explanation=_safe_explanation(candidate),
    )


def generate_action_proposal(
    db: Session,
    *,
    conversation_id: int,
    content: str,
    timezone: str,
    provider: LLMProvider,
    clock: Clock,
) -> tuple[ActionProposalOut, LLMResponse | None]:
    """Generate a preview without writing any message or domain record."""
    get_conversation(db, conversation_id)

    if any(pattern.search(content) for pattern in UNSUPPORTED_PATTERNS):
        return _unsupported(), None

    timezone_info = ZoneInfo(timezone)
    now_utc = clock.now()
    system_prompt = build_action_proposal_system_prompt(
        current_date=now_utc.astimezone(timezone_info).strftime("%Y-%m-%d"),
        user_timezone=timezone,
    )
    history = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .all()
    )
    provider_messages = build_provider_messages(history, system_prompt=system_prompt)
    provider_messages.append({"role": "user", "content": build_action_proposal_user_prompt(content)})
    response = provider.generate(provider_messages, response_schema=ActionProposalCandidate)

    try:
        raw = json.loads(response.text)
        if not isinstance(raw, dict):
            return _invalid(), response
        candidate = ActionProposalCandidate.model_validate(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _invalid(), response

    return _build_validated(
        candidate,
        content=content,
        timezone=timezone,
        now_utc=now_utc,
    ), response


def usage_values(response: LLMResponse | None) -> tuple[int | None, int | None, int | None]:
    return _usage(response)
