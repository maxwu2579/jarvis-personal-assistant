"""测试夹具：每个测试使用独立的临时 SQLite 数据库，绝不触碰开发库。

LLM 层固定使用 FakeLLMProvider —— 测试期间绝不连接真实模型或互联网。
时钟固定为 FIXED_NOW（UTC），测试不依赖真实当前时间。

数据库隔离（Phase 4B 强化）：
- 本模块顶层在导入任何 app 模块【之前】设置 DATABASE_URL / DOCUMENT_STORAGE_DIR
  环境变量（pydantic-settings 会读取它们）。这保证 app.core.database 的模块级
  engine 与 TestClient lifespan 的 create_all 都指向会话级临时库，
  而不是真实 backend/jarvis.db（Phase 4A 曾发现 lifespan create_all 在
  真实库上建表）。
- setdefault：开发者显式设置的环境变量（如指向自己的测试库）优先；
  默认值 `sqlite:///./jarvis.db`（相对 cwd 的真实库）被覆盖。
- 每个测试的 client 夹具仍使用独立 tmp_path 库（业务数据隔离）；
  会话级临时库只承载 lifespan 的 schema 初始化。
- 会话结束时清理临时目录。
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# ---- 必须先于任何 app 导入设置环境变量（见模块 docstring）----
_TEST_SESSION_DIR = Path(tempfile.mkdtemp(prefix="jarvis_test_session_"))
os.environ.setdefault(
    "DATABASE_URL", f"sqlite:///{_TEST_SESSION_DIR.as_posix()}/session.db"
)
os.environ.setdefault("DOCUMENT_STORAGE_DIR", str(_TEST_SESSION_DIR / "documents"))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_session_dir():
    """会话结束后清理会话级临时目录（含 lifespan 初始化过的 session.db）。"""
    yield
    shutil.rmtree(_TEST_SESSION_DIR, ignore_errors=True)


from app.core.clock import FixedClock, get_clock  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, get_db, _set_sqlite_pragma  # noqa: E402
from app.llm.fake_provider import FakeLLMProvider  # noqa: E402
from app.llm.factory import get_llm_provider  # noqa: E402
from app.main import app  # noqa: E402

from app.services.reminder_service import process_due_reminders_once  # noqa: E402

# 固定时刻：2026-08-11 04:00 UTC == 2026-08-11 12:00 Asia/Shanghai
FIXED_NOW = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def fake_provider():
    return FakeLLMProvider()


@pytest.fixture()
def client(fake_provider, tmp_path):
    db_path = tmp_path / "test_tasks.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    # 与生产引擎一致：SQLite 默认不启用外键，必须显式开启（CASCADE/拒绝插入才生效）
    event.listen(test_engine, "connect", _set_sqlite_pragma)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_clock] = lambda: FixedClock(FIXED_NOW)
    try:
        # raise_server_exceptions=False：服务端异常以真实 500 响应暴露
        # （TestClient 默认会 re-raise，与生产 uvicorn 行为不一致）
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def worker_env(client, tmp_path):
    """提供与 client 同一临时库的独立 session（模拟独立 Worker 进程）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test_tasks.db'}", connect_args={"check_same_thread": False}
    )
    # 与 client 夹具一致：外键约束必须真实生效，否则 CASCADE/拒绝行为无法被测试
    event.listen(engine, "connect", _set_sqlite_pragma)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionLocal


def run_worker_once(worker_env, clock, **kwargs):
    """以独立 Worker 会话执行单次处理（测试共用）。"""
    engine, SessionLocal = worker_env
    db = SessionLocal()
    try:
        return process_due_reminders_once(db, clock, **kwargs)
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _isolated_document_storage(tmp_path, monkeypatch):
    """所有测试共享：文档存储目录重定向到测试临时目录，绝不触碰 data/documents。

    autouse 保证任何测试（无论是否直接测文档）都不会把文件写进项目目录。
    """
    storage = tmp_path / "documents"
    monkeypatch.setattr(settings, "document_storage_dir", str(storage))
    return storage
