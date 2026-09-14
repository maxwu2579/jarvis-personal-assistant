"""FastAPI 应用入口。

错误约定：所有错误响应使用统一结构 {"error": {"code": ..., "message": ...}}，
包括 Pydantic/FastAPI 的 422 校验错误（经 RequestValidationError handler 归一）。
Phase 6E 的 Execution 错误信封额外带 details 与 correlation_id：
{"error": {"code", "message", "details", "correlation_id"}}——旧 API 信封
保持不变（前端 parseError 只提取 code/message，两种格式均兼容）。
内部错误只记录日志，不向客户端泄露堆栈或数据库内部信息。
"""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

import app.models  # noqa: F401  注册所有模型
from app.api.conversations import router as conversations_router
from app.api.documents import router as documents_router
from app.api.executions import router as executions_router
from app.api.rag import router as rag_router
from app.api.reminders import router as reminders_router
from app.api.retrieval import router as retrieval_router
from app.api.tasks import router as tasks_router
from app.api.calendar import router as calendar_router, calendar_error_handler
from app.integrations.calendar.errors import CalendarError
from app.integrations.calendar.security import CalendarCallbackPrivacy, secure_library_logging
from app.core.config import settings
from app.core.database import IS_SQLITE, Base, engine, get_db
from app.llm.base import LLMNotConfiguredError, LLMTimeoutError, LLMUpstreamError
from app.services.chat_service import ConversationNotFoundError
from app.services.document_errors import DOCUMENT_ERROR_STATUS, DocumentError
from app.services.execution_errors import (
    EXECUTION_ERROR_STATUS,
    ExecutionError,
)
from app.services.proposal_service import ProposalSchemaError
from app.services.rag_errors import RAG_ERROR_STATUS, RAGError
from app.services.retrieval_errors import RETRIEVAL_ERROR_STATUS, RetrievalError
from app.services.task_service import InvalidTransitionError, TaskNotFoundError

logger = logging.getLogger(__name__)


def _correlation_id(request: Request) -> str | None:
    """request.state.correlation_id（CorrelationIdMiddleware 注入）。"""
    return getattr(request.state, "correlation_id", None)


class CorrelationIdMiddleware:
    """correlation ID 注入（Phase 6E observability）。

    - 透传客户端 X-Request-ID / X-Correlation-ID；缺失则生成 uuid4 hex；
    - 注入 scope["state"]（FastAPI 经 request.state 读取），事件与错误
      信封共用同一 ID，客户端可跨请求关联；
    - 响应头 X-Request-ID 回显；不记录请求体（安全日志边界）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cid = None
        for name, value in scope.get("headers", []):
            if name in (b"x-request-id", b"x-correlation-id"):
                cid = value.decode("latin-1").strip()
                break
        if not cid:
            cid = uuid.uuid4().hex
        scope.setdefault("state", {})["correlation_id"] = cid

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"X-Request-ID", cid.encode("latin-1"))
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _ensure_schema_initialized() -> None:
    """开发模式自动建表，并检测 create_all 与 Alembic 双路径错位。

    create_all 创建的库没有 alembic_version 表；此时若直接 `alembic upgrade head`
    会因「table already exists」响亮失败（不会静默跳过迁移）。这里在启动时给出
    明确指引：`alembic stamp head` 把现有 schema 标记为当前版本，之后可正常迁移。
    """
    Base.metadata.create_all(bind=engine)
    if IS_SQLITE:
        from sqlalchemy import inspect

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        has_tables = bool(tables & {"tasks", "reminders", "conversations"})
        if has_tables and "alembic_version" not in tables:
            logger.warning(
                "database was created by create_all and has no Alembic version. "
                "Before using `alembic upgrade head`, run `alembic stamp head` "
                "in backend/ to align it (see README '数据库初始化')."
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Phase 0 策略：开发环境启动时自动建表（幂等）。
    # 正式的数据库初始化路径是 Alembic（backend/alembic，`alembic upgrade head`），
    # 与 create_all 共用同一份 Base.metadata，schema 保持一致。
    # 进入生产部署时切换到 Alembic 并移除 create_all（见 README「数据库初始化」）。
    _ensure_schema_initialized()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# CORS：白名单来自环境变量 CORS_ORIGINS（默认本地 Vite 开发地址）。
# 明确不启用 allow_credentials —— 通配方法/头与显式来源白名单搭配是安全的，
# 但绝不与 credentials 同时启用通配来源。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(CalendarCallbackPrivacy)
app.add_exception_handler(CalendarError, calendar_error_handler)
secure_library_logging()


def _format_validation_message(errors: list[dict]) -> str:
    parts = []
    for error in errors:
        loc = ".".join(str(part) for part in error.get("loc", []))
        msg = error.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError):
    """422 校验错误归一为统一错误结构（含 naive 时区、空白 title 等业务校验）。"""
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", _format_validation_message(exc.errors())),
    )


@app.exception_handler(TaskNotFoundError)
async def task_not_found_handler(_: Request, exc: TaskNotFoundError):
    return JSONResponse(
        status_code=404,
        content=_error_body("TASK_NOT_FOUND", f"Task {exc.task_id} does not exist"),
    )


@app.exception_handler(InvalidTransitionError)
async def invalid_transition_handler(_: Request, exc: InvalidTransitionError):
    return JSONResponse(status_code=409, content=_error_body("INVALID_TRANSITION", str(exc)))


@app.exception_handler(ConversationNotFoundError)
async def conversation_not_found_handler(_: Request, exc: ConversationNotFoundError):
    return JSONResponse(
        status_code=404,
        content=_error_body(
            "CONVERSATION_NOT_FOUND", f"Conversation {exc.conversation_id} does not exist"
        ),
    )


@app.exception_handler(LLMNotConfiguredError)
async def llm_not_configured_handler(_: Request, exc: LLMNotConfiguredError):
    # 配置缺失属于服务端问题，但文案必须稳定清晰，不涉及任何密钥
    logger.warning("llm.not_configured: %s", exc)
    return JSONResponse(
        status_code=503,
        content=_error_body("LLM_NOT_CONFIGURED", "LLM is not configured on this server"),
    )


@app.exception_handler(LLMTimeoutError)
async def llm_timeout_handler(_: Request, exc: LLMTimeoutError):
    # 超时时间本身不敏感，但统一走稳定文案
    logger.warning("llm.timeout: %s", exc)
    return JSONResponse(
        status_code=502,
        content=_error_body("LLM_TIMEOUT", "LLM provider timed out"),
    )


@app.exception_handler(LLMUpstreamError)
async def llm_upstream_error_handler(_: Request, exc: LLMUpstreamError):
    # 绝不向客户端暴露上游原始响应体 / 内部异常
    logger.warning("llm.upstream_error: %s", exc)
    return JSONResponse(
        status_code=502,
        content=_error_body("LLM_UPSTREAM_ERROR", "LLM provider returned an error"),
    )


@app.exception_handler(ProposalSchemaError)
async def proposal_schema_error_handler(_: Request, exc: ProposalSchemaError):
    # 模型输出未通过校验：不创建任务，不泄露模型原始输出
    logger.warning("proposal.schema_error: %s", exc)
    return JSONResponse(
        status_code=502,
        content=_error_body(
            "LLM_SCHEMA_ERROR", "LLM returned an invalid task proposal"
        ),
    )


@app.exception_handler(DocumentError)
async def document_error_handler(_: Request, exc: DocumentError):
    """文档领域错误 → 稳定状态码（错误码清单见 document_errors.py）。"""
    status = DOCUMENT_ERROR_STATUS.get(exc.code, 500)
    if status >= 500:
        logger.warning("document.error: %s", exc)
    return JSONResponse(status_code=status, content=_error_body(exc.code, exc.message))


@app.exception_handler(RetrievalError)
async def retrieval_error_handler(_: Request, exc: RetrievalError):
    """检索领域错误 → 稳定状态码（错误码清单见 retrieval_errors.py）。

    索引失败（INDEXING_FAILED）已把文档置为 INDEX_FAILED 并回滚事务，
    此处仍返回 500：调用方必须显式知道索引没有成功。
    """
    status = RETRIEVAL_ERROR_STATUS.get(exc.code, 500)
    if status >= 500:
        logger.warning("retrieval.error: %s", exc)
    return JSONResponse(status_code=status, content=_error_body(exc.code, exc.message))


@app.exception_handler(RAGError)
async def rag_error_handler(_: Request, exc: RAGError):
    """RAG 领域错误 → 稳定状态码（错误码清单见 rag_errors.py）。

    只记录日志（含可审计的脱敏细节），客户端只收到固定文案：
    不泄露模型原始输出、完整 system prompt、路径、Traceback 或密钥。
    """
    status = RAG_ERROR_STATUS.get(exc.code, 502)
    logger.warning(
        "rag.error: %s detail=%s", exc.code, getattr(exc, "error_detail", "")
    )
    return JSONResponse(status_code=status, content=_error_body(exc.code, exc.message))


@app.exception_handler(ExecutionError)
async def execution_error_handler(request: Request, exc: ExecutionError):
    """Execution 领域错误（Phase 6E）→ 稳定状态码（清单见 execution_errors.py）。

    错误信封带 details 与 correlation_id（规格十）：
    {"error": {"code", "message", "details", "correlation_id"}}。
    message 为 service 层脱敏文案；不泄露堆栈/数据库内部信息。
    """
    status = EXECUTION_ERROR_STATUS.get(exc.code, 500)
    cid = _correlation_id(request) or ""
    if status >= 500:
        logger.warning("execution.error: %s", exc)
    else:
        logger.info("execution.error code=%s correlation_id=%s", exc.code, cid)
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": {},
                "correlation_id": cid,
            }
        },
    )


@app.exception_handler(OperationalError)
async def database_unavailable_handler(_: Request, exc: OperationalError):
    """数据库不可用（连接失败/锁定等）→ 稳定 503。

    Phase 5A：PG 不可用（或 SQLite 锁定）必须返回稳定可读的脱敏错误——
    响应绝不包含连接 URL、用户名、密码、SQL 原文或堆栈；日志只记异常类型。
    """
    logger.warning("db.unavailable: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content=_error_body("SERVICE_UNAVAILABLE", "Database is unavailable"),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底：堆栈与内部细节只进日志，客户端只收到稳定的 500。

    Phase 6F 修复：FastAPI 把 Exception handler 安装在最外层的
    ServerErrorMiddleware 上，其错误响应经原始 send 直接发出、绕过
    CorrelationIdMiddleware —— 此前 500 响应缺失 X-Request-ID。
    这里在响应头补回 correlation_id（body 保持旧两字段信封不变，
    兼容既有 detail.error 读取方式）。
    """
    logger.exception("Unhandled error: %s", exc)
    cid = _correlation_id(request)
    return JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", "Internal server error"),
        headers={"X-Request-ID": cid} if cid else None,
    )


app.include_router(tasks_router)
app.include_router(conversations_router)
app.include_router(reminders_router)
app.include_router(documents_router)
app.include_router(retrieval_router)
app.include_router(rag_router)
app.include_router(executions_router)
app.include_router(calendar_router)


@app.get("/", tags=["meta"])
def root():
    return {"app": settings.app_name, "docs": "/docs", "openapi": "/openapi.json"}


@app.get("/health", tags=["meta"])
def health():
    """存活探针（liveness）：不访问数据库，进程活着即 200。

    Phase 5A 拆分：应用存活 ≠ 数据库可用；数据库可用性由 /health/db 提供。
    """
    return {"status": "ok"}


@app.get("/health/db", tags=["meta"])
def health_db(db: Session = Depends(get_db)):
    """数据库可用性探针（readiness）。

    - SQLite：SELECT 1 验证连接；
    - PostgreSQL：pgvector 能力完整检查，稳定状态码（Phase 5B）：
      DB_UNAVAILABLE / MIGRATION_NOT_CURRENT / VECTOR_EXTENSION_MISSING /
      VECTOR_SCHEMA_MISSING / VECTOR_DIMENSION_MISMATCH；
    - 响应不含 URL/密码/内部异常；失败返回稳定脱敏 503。
    """
    if IS_SQLITE:
        try:
            db.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content=_error_body("DB_UNAVAILABLE", "Database is unavailable"),
            )
        return {"status": "ok", "database": "available"}
    from app.services.vector_readiness import (
        check_pg_vector_readiness,
        readiness_message,
    )

    code = check_pg_vector_readiness(db, settings.embedding_dimension)
    if code is None:
        return {
            "status": "ok",
            "database": "available",
            "pgvector": "ready",
        }
    return JSONResponse(
        status_code=503,
        content=_error_body(code, readiness_message(code)),
    )


@app.get("/health/embedding", tags=["meta"])
def health_embedding(db: Session = Depends(get_db)):
    """Embedding 生产就绪探针（Phase 5C，7 态）。

    ready / provider_config_invalid / provider_key_missing /
    vector_extension_missing / vector_schema_missing /
    vector_dimension_mismatch / embedding_index_stale；
    DB 不可达额外如实上报 database_unavailable。

    诚实边界：verified 恒为 False（本端点不做在线 smoke，不自动消费
    API 额度；真实语义验证由 RUN_LIVE_EMBEDDING_TESTS 门控测试完成）。
    响应不含 Key/URL/向量/原文；非 ready 返回稳定脱敏 503。
    """
    from app.services.embedding_health import check_embedding_health

    payload = check_embedding_health(db)
    if payload["status"] != "ready":
        return JSONResponse(status_code=503, content=payload)
    return payload
