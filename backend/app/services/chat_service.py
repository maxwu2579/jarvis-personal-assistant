"""对话编排：持久化 + LLM 调用的顺序编排。

失败语义（保证数据库状态明确）：
1. USER 消息先落库并 commit —— 无论模型调用结果如何，用户说了什么是确定的；
2. 模型调用失败 → 抛 LLM*Error（不落库任何 ASSISTANT 占位），
   调用方（API 层）返回统一错误；库里只保留 USER 消息，GET messages 可核对；
3. 模型调用成功 → ASSISTANT 消息落库并 commit，随后 touch conversation.updated_at。

不执行自动重试：生成型调用不视为幂等。
"""

import logging
import time

from sqlalchemy.orm import Session

from app.llm.base import LLMProvider, LLMResponse
from app.llm.messages import build_provider_messages
from app.models.conversation import Conversation, Message, MessageRole
from app.models.task import utcnow

logger = logging.getLogger(__name__)


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
) -> tuple[Message, Message, LLMResponse]:
    """保存 USER 消息 → 调用 Provider → 保存 ASSISTANT 消息。"""
    get_conversation(db, conversation_id)  # 不存在则 404，什么也不写

    user_message = Message(
        conversation_id=conversation_id, role=MessageRole.USER.value, content=content
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
    provider_messages = build_provider_messages(history)

    # LLM 调用不重试；任何失败向上抛 LLM*Error，由 API 层统一映射
    started = time.monotonic()
    response = provider.generate(provider_messages)
    latency_ms = int((time.monotonic() - started) * 1000)
    if response.latency_ms is None:
        response.latency_ms = latency_ms

    assistant_message = Message(
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT.value,
        content=response.text,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        latency_ms=response.latency_ms,
    )
    db.add(assistant_message)

    # 对话活动 → 刷新 updated_at，保证列表排序有意义
    conversation = db.get(Conversation, conversation_id)
    if conversation is not None:
        conversation.updated_at = utcnow()

    db.commit()
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
    return user_message, assistant_message, response
