"""对话编排：持久化 + LLM 调用的顺序编排。

失败语义（保证数据库状态明确）：
1. USER 消息先落库并 commit —— 无论模型调用结果如何，用户说了什么是确定的；
2. 模型调用失败 → 抛 LLM*Error（不落库任何 ASSISTANT 占位），
   调用方（API 层）返回统一错误；库里只保留 USER 消息，GET messages 可核对；
3. 模型调用成功 → ASSISTANT 消息落库并 commit，随后 touch conversation.updated_at。

不执行自动重试：生成型调用不视为幂等。

Phase 10C（普通 Chat 的显式、单次、只读 Memory 上下文）：
- 被标记（memory_context_applied）的历史回复由 llm/messages.py 统一排除；
- Memory 快照在 USER 落库与历史组装之后、Provider 调用之前，用独立的短读会话
  读取并在调用 Provider 前完全关闭；可选上下文读取失败不影响普通 Chat；
- ASSISTANT 回复与来源标记在同一事务中落库；应用层每次发送至多调用一次 Provider；
- messages 资源所需的 DB 读写统一走脱敏边界：OperationalError → 既有 503，
  其他 SQLAlchemyError → ChatPersistenceError（500），不记录异常文本 / SQL 参数 / 回复。
"""

import logging
import time
from contextlib import contextmanager

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.llm.base import LLMProvider, LLMResponse
from app.llm.messages import build_provider_messages
from app.models.conversation import Conversation, Message, MessageRole
from app.models.task import utcnow
from app.services.memory_context_service import (
    STATUS_APPLIED,
    MemoryContext,
    select_memory_context,
)

logger = logging.getLogger(__name__)


class ChatPersistenceError(Exception):
    """映射为 500 + CHAT_PERSISTENCE_FAILED。不携带任何原始异常文本。"""

    def __init__(self) -> None:
        super().__init__("Chat persistence failed")


@contextmanager
def _chat_db_boundary(db: Session):
    """messages 资源所需 DB 访问的脱敏失败边界（读与写）。"""
    try:
        yield
    except OperationalError:
        _safe_rollback(db)
        raise  # 既有脱敏 handler → 503 SERVICE_UNAVAILABLE（只记录异常类型名）
    except SQLAlchemyError:
        _safe_rollback(db)
        logger.warning("chat.persistence_failed")
        raise ChatPersistenceError() from None


def _safe_rollback(db: Session) -> None:
    try:
        db.rollback()
    except SQLAlchemyError:
        logger.warning("chat.rollback_failed")


class ConversationNotFoundError(Exception):
    """映射为 404 + CONVERSATION_NOT_FOUND。"""

    def __init__(self, conversation_id: int):
        self.conversation_id = conversation_id
        super().__init__(f"Conversation {conversation_id} does not exist")


def create_conversation(db: Session, *, title: str | None) -> Conversation:
    conversation = Conversation(title=title)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def list_conversations(db: Session) -> list[Conversation]:
    return db.query(Conversation).order_by(Conversation.updated_at.desc()).all()


def get_conversation(db: Session, conversation_id: int) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise ConversationNotFoundError(conversation_id)
    return conversation


def list_messages(db: Session, conversation_id: int) -> list[Message]:
    """本地完整对话记录（含被标记的回复）；不做 Provider 历史过滤。"""
    with _chat_db_boundary(db):
        get_conversation(db, conversation_id)  # 先验证存在
        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )


def send_message(
    db: Session,
    conversation_id: int,
    content: str,
    provider: LLMProvider,
    memory_keys: tuple[str, ...] | list[str] = (),
) -> tuple[Message, Message, LLMResponse, MemoryContext]:
    """保存 USER 消息 → 调用 Provider → 保存 ASSISTANT 消息（含来源标记）。"""
    with _chat_db_boundary(db):
        get_conversation(db, conversation_id)  # 不存在则 404，什么也不写

        user_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.USER.value,
            content=content,
            memory_context_applied=False,
        )
        db.add(user_message)
        db.commit()
        db.refresh(user_message)

        history = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        # 共享历史构建器：只保留来源标记被证明为合法 false 的行
        history_messages = build_provider_messages(history)

    # Memory 快照（线性化点）：独立短读会话，绑定本请求的同一 DB engine，返回前已关闭。
    # 永不抛出、永不写入；不选择任何 key 时不产生任何 Memory 查询。
    memory_context = select_memory_context(db.get_bind(), memory_keys)
    provider_messages = [*memory_context.provider_messages(), *history_messages]
    marked = memory_context.status == STATUS_APPLIED

    # LLM 调用不重试；任何失败向上抛 LLM*Error，由 API 层统一映射
    started = time.monotonic()
    response = provider.generate(provider_messages)
    latency_ms = int((time.monotonic() - started) * 1000)
    if response.latency_ms is None:
        response.latency_ms = latency_ms

    # 回复与来源标记同一事务；失败则回滚并返回脱敏错误，绝不重试、不二次生成。
    with _chat_db_boundary(db):
        assistant_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT.value,
            content=response.text,
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
            memory_context_applied=marked,
        )
        db.add(assistant_message)

        # 对话活动 → 刷新 updated_at，保证列表排序有意义
        conversation = db.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = utcnow()

        db.commit()
        db.refresh(user_message)
        db.refresh(assistant_message)

    logger.info(
        "chat.exchange_completed",
        extra={
            "conversation_id": conversation_id,
            "user_message_id": user_message.id,
            "assistant_message_id": assistant_message.id,
            "model": response.model,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "latency_ms": response.latency_ms,
            "history_messages": len(provider_messages),
        },
    )
    return user_message, assistant_message, response, memory_context
