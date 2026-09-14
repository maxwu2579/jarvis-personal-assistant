"""测试数据库隔离回归（Phase 4B）：任何测试都不得触碰真实开发库。

Phase 4A 曾发现：TestClient 的 lifespan 会对 app.core.database 的模块级
engine 执行 create_all，而该 engine 默认绑定真实 backend/jarvis.db
（settings.database_url 默认 sqlite:///./jarvis.db，相对 cwd 解析）。

修复：conftest.py 在导入任何 app 模块之前 setdefault DATABASE_URL /
DOCUMENT_STORAGE_DIR 到会话级临时目录。本文件用回归测试钉死这一保证：

1. module engine 绝不指向真实库路径；
2. TestClient 完整业务请求前后，真实库的 SHA-256 / mtime / size 三元组不变；
   若真实库不存在，则测试结束后仍不存在（测试不得创建它）；
3. 子进程（cwd=临时目录、默认配置）触发 lifespan 时，create_all 只作用于
   「当前 cwd 下的 jarvis.db」，不产生任何指向真实库的写入；
4. 测试代码不加载 dotenv（不读取 .env 文件）。
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REAL_DB = BACKEND_DIR / "jarvis.db"


def _db_fingerprint(path: Path) -> tuple[str, int, int] | None:
    """真实库三要素：SHA-256、mtime（纳秒）、size；不存在返回 None。"""
    if not path.exists():
        return None
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_mtime_ns,
        path.stat().st_size,
    )


def test_app_engine_never_points_to_real_dev_db():
    """模块级 engine 必须绑定会话级临时库，而非真实 backend/jarvis.db。"""
    from app.core.database import engine

    engine_db = Path(engine.url.database).resolve()
    assert engine_db != REAL_DB.resolve(), (
        f"module engine points at the real dev database: {engine_db}"
    )
    assert "jarvis_test_session" in str(engine_db)


def test_testclient_requests_leave_real_db_untouched(client):
    """完整业务请求（health + 文档上传 + 删除）前后真实库三要素不变。"""
    before = _db_fingerprint(REAL_DB)

    r = client.get("/health")
    assert r.status_code == 200
    files = {"file": ("iso.txt", "隔离测试文档内容 2026".encode("utf-8"), "text/plain")}
    r = client.post("/api/documents", files=files)
    assert r.status_code == 201
    doc_id = r.json()["id"]
    r = client.delete(f"/api/documents/{doc_id}")
    assert r.json() == {"deleted": True}

    after = _db_fingerprint(REAL_DB)
    if before is None:
        # 真实库不存在：测试不得创建它
        assert after is None, "tests created the real dev database"
    else:
        assert after is not None
        assert after[0] == before[0], "real dev database SHA-256 changed"
        assert after[1] == before[1], "real dev database mtime changed"
        assert after[2] == before[2], "real dev database size changed"


def test_lifespan_create_all_is_scoped_to_cwd(tmp_path):
    """子进程（默认 DATABASE_URL、cwd=临时目录）触发 lifespan 时，
    create_all 只创建「当前目录」的 jarvis.db，绝不写真实 backend/jarvis.db。"""
    before = _db_fingerprint(REAL_DB)

    script = (
        "import os, sys\n"
        "os.environ.pop('DATABASE_URL', None)\n"
        "os.environ.pop('DOCUMENT_STORAGE_DIR', None)\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "os.chdir(sys.argv[1])\n"
        "from app.main import app\n"
        "from fastapi.testclient import TestClient\n"
        "with TestClient(app) as c:\n"
        "    r = c.get('/health')\n"
        "    assert r.status_code == 200, r.text\n"
        "print('subprocess-ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(BACKEND_DIR)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "subprocess-ok" in proc.stdout

    # 默认 URL 相对 cwd：临时目录下被创建了 jarvis.db（证明 lifespan 起作用）
    assert (tmp_path / "jarvis.db").exists()

    after = _db_fingerprint(REAL_DB)
    if before is None:
        assert after is None, "subprocess created the real dev database"
    else:
        assert after == before, "real dev database changed by subprocess lifespan"


def test_tests_do_not_load_dotenv():
    """测试代码不得加载 .env（无 dotenv 用法；本文件自身除外）。"""
    tests_dir = Path(__file__).parent
    for py_file in tests_dir.glob("*.py"):
        if py_file.name == Path(__file__).name:
            continue  # 本文件包含断言用的字样
        source = py_file.read_text(encoding="utf-8")
        assert "dotenv" not in source, f"{py_file.name} uses dotenv"
        assert "load_dotenv" not in source, f"{py_file.name} calls load_dotenv"
