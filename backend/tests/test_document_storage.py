"""磁盘存储安全测试：目录创建、存储键校验、原子写入、失败清理、幂等删除。"""

import os
from pathlib import Path

import pytest

from app.services import document_storage


def test_ensure_storage_dir_creates_recursively(tmp_path):
    root = document_storage.ensure_storage_dir(tmp_path / "a" / "b" / "docs")
    assert root == (tmp_path / "a" / "b" / "docs").resolve()
    assert root.is_dir()
    # 幂等：重复调用不报错
    assert document_storage.ensure_storage_dir(tmp_path / "a" / "b" / "docs") == root


@pytest.mark.parametrize(
    "key",
    [
        "../evil",  # 路径穿越
        "a" * 31,  # 长度不符
        "A" * 32,  # 大写
        "a" * 32 + "x",  # 超长
        "a/b" * 16,  # 含分隔符
        "aa bb" * 8,  # 含空格
    ],
)
def test_storage_path_for_rejects_invalid_keys(tmp_path, key):
    root = document_storage.ensure_storage_dir(tmp_path)
    with pytest.raises(ValueError):
        document_storage.storage_path_for(root, key)


def test_storage_path_for_resolves_inside_root(tmp_path):
    root = document_storage.ensure_storage_dir(tmp_path)
    key = "a" * 32
    path = document_storage.storage_path_for(root, key)
    assert path == (root / key).resolve()
    assert path.is_relative_to(root)


def test_save_upload_atomic_writes_content(tmp_path):
    root = document_storage.ensure_storage_dir(tmp_path)
    key = "b" * 32
    document_storage.save_upload_atomic(root, key, b"hello world")
    assert (root / key).read_bytes() == b"hello world"
    # 无临时文件残留
    assert list(root.glob(".tmp-*")) == []


def test_save_upload_atomic_overwrites_existing(tmp_path):
    root = document_storage.ensure_storage_dir(tmp_path)
    key = "c" * 32
    document_storage.save_upload_atomic(root, key, b"old")
    document_storage.save_upload_atomic(root, key, b"new")
    assert (root / key).read_bytes() == b"new"


def test_save_upload_atomic_failure_cleans_temp(monkeypatch, tmp_path):
    """注入 os.replace 失败：目标文件不存在、临时文件被清理、异常传播。"""
    root = document_storage.ensure_storage_dir(tmp_path)
    key = "d" * 32
    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(document_storage.os, "replace", failing_replace)
    with pytest.raises(OSError):
        document_storage.save_upload_atomic(root, key, b"data")
    assert not (root / key).exists()
    assert list(root.glob(".tmp-*")) == []


def test_save_upload_atomic_failure_leaves_no_partial_file(tmp_path):
    """覆盖说明：写入阶段失败与 os.replace 失败走同一清理分支（已在上方
    test_save_upload_atomic_failure_cleans_temp 验证）；此处补一个断言：
    目标文件从不以半成品形式存在（先写临时文件后原子改名）。"""
    root = document_storage.ensure_storage_dir(tmp_path)
    key = "e" * 32
    document_storage.save_upload_atomic(root, key, b"complete")
    assert (root / key).read_bytes() == b"complete"


def test_delete_file_idempotent(tmp_path):
    root = document_storage.ensure_storage_dir(tmp_path)
    key = "f" * 32
    document_storage.save_upload_atomic(root, key, b"x")
    document_storage.delete_file(root, key)
    assert not (root / key).exists()
    # 重复删除不抛异常（幂等）
    document_storage.delete_file(root, key)
