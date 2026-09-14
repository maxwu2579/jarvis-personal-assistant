"""模型注册：确保 create_all 与 Alembic autogenerate 能看到所有表定义。"""

from app.models.conversation import Conversation, Message, MessageRole
from app.models.document import (
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentIndexStatus,
    DocumentStatus,
)
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_attempt import (
    AttemptStatus,
    ExecutionAttempt,
    LIVE_ATTEMPT_STATUSES,
    TERMINAL_ATTEMPT_STATUSES,
)
from app.models.execution_event import ExecutionEvent
from app.models.execution_step import ConfirmationStatus, ExecutionStep, StepStatus
from app.models.proposal import TaskProposal, TaskProposalStatus
from app.models.rag_audit import RagAnswer, RagAnswerStatus
from app.models.reminder import Notification, NotificationType, Reminder, ReminderStatus
from app.models.task import Task, TaskStatus

__all__ = [
    "AttemptStatus",
    "ChunkEmbedding",
    "Conversation",
    "Document",
    "DocumentChunk",
    "DocumentIndexStatus",
    "DocumentStatus",
    "ConfirmationStatus",
    "Execution",
    "ExecutionAttempt",
    "ExecutionEvent",
    "ExecutionStatus",
    "ExecutionStep",
    "LIVE_ATTEMPT_STATUSES",
    "TERMINAL_ATTEMPT_STATUSES",
    "Message",
    "MessageRole",
    "Notification",
    "NotificationType",
    "RagAnswer",
    "RagAnswerStatus",
    "Reminder",
    "ReminderStatus",
    "StepStatus",
    "Task",
    "TaskProposal",
    "TaskProposalStatus",
    "TaskStatus",
]
