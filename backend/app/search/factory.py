"""SearchBackend 装配：按当前数据库方言返回对应实现（进程级缓存）。

方言在 database.py 模块加载时确定一次（DIALECT），后端随引擎固定，
不需要每次查询重新判断。
"""

from functools import lru_cache

from app.core.database import DIALECT
from app.core.dialect import DatabaseDialect
from app.search.base import SearchBackend


@lru_cache
def get_search_backend() -> SearchBackend:
    """返回当前方言的检索后端（SQLite=FTS5 / PG=ILIKE 回退）。"""
    if DIALECT is DatabaseDialect.SQLITE:
        from app.search.sqlite_backend import SQLiteSearchBackend

        return SQLiteSearchBackend()
    from app.search.postgresql_backend import PostgreSQLSearchBackend

    return PostgreSQLSearchBackend()
