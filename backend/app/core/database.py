"""SQLAlchemy 引擎与会话管理。

Phase 5A：方言识别集中在本模块（DIALECT / IS_SQLITE），业务服务
一律从本模块或 app.core.dialect 取方言能力，禁止散落字符串判断。
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings
from app.core.dialect import DatabaseDialect, detect_dialect

DIALECT = detect_dialect(settings.database_url)
IS_SQLITE = DIALECT is DatabaseDialect.SQLITE

# SQLite 多线程访问需要 check_same_thread=False（FastAPI 在多个线程中调用依赖）
connect_args = {"check_same_thread": False} if IS_SQLITE else {}

engine_kwargs: dict = {"connect_args": connect_args}
if not IS_SQLITE:
    # PostgreSQL：连接池预检（pool_pre_ping）——自动丢弃陈旧连接，
    # 数据库重启后无需应用重启即可恢复。SQLite 保持本地配置不变。
    engine_kwargs["pool_pre_ping"] = True
engine = create_engine(settings.database_url, **engine_kwargs)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """SQLite 默认不启用外键约束——必须显式开启，CASCADE 才会生效。

    这是所有外键行为（删除级联、插入拒绝）的前提；测试会直接验证。
    PostgreSQL 外键默认强制，无需 pragma。
    """
    if IS_SQLITE:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


if not IS_SQLITE:
    try:
        from pgvector.psycopg import register_vector  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - pgvector 缺失时 PG 向量路径不可用
        register_vector = None

    @event.listens_for(engine, "connect")
    def _register_pg_vector_adapter(dbapi_connection, connection_record):
        """PostgreSQL：为每个连接注册 pgvector 类型适配器（list[float] <-> vector）。

        Phase 5B：原生向量写入/绑定查询依赖 psycopg 3 对 vector 的编解码。
        SQLite 分支绝不 import pgvector 适配器（零依赖路径保持零依赖）。
        """
        if register_vector is not None:
            register_vector(dbapi_connection)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖：为每个请求提供一个独立 session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
