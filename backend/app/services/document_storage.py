"""文档磁盘存储：路径安全与原子写入。

安全约定（与 README「安全文件存储」一致）：
- 存储目录由服务端配置（document_storage_dir），文件必须落在该目录内；
- 存储键是服务端生成的 UUID hex（无扩展名、无路径分隔符），
  原始文件名永不参与路径计算；
- 每次写入走「同目录临时文件 + os.replace」原子落盘，
  失败时清理临时文件，不留半成品；
- 路径解析使用 Path.resolve() + is_relative_to() 双重防御，
  杜绝符号链接/../ 逃逸。
"""

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_storage_dir(storage_dir: str | Path) -> Path:
    """解析并创建存储目录（幂等）。返回规范化后的绝对路径。"""
    root = Path(storage_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def storage_path_for(root: Path, storage_key: str) -> Path:
    """把存储键映射为存储目录内的绝对路径；非法键（路径分隔符等）直接拒绝。

    存储键必须是 UUID hex（32 个 [0-9a-f]），此处不再假设调用方正确，
    防御性校验任何形如 ../x 或含分隔符的输入。
    """
    if len(storage_key) != 32 or not storage_key.isalnum() or not storage_key.islower():
        raise ValueError(f"invalid storage key: {storage_key!r}")
    candidate = (root / storage_key).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"storage key escapes storage dir: {storage_key!r}")
    return candidate


def save_upload_atomic(root: Path, storage_key: str, content: bytes) -> Path:
    """原子写入：先写同目录临时文件，再 os.replace 到目标名。

    进程崩溃时只会残留 .tmp 文件（目标名永远不存在半成品）；
    调用方负责在 DB 失败时清理目标文件。
    """
    target = storage_path_for(root, storage_key)
    tmp = target.with_name(f".tmp-{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # 任何失败（磁盘满/中断/权限）都清理临时文件，不掩盖原始错误
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def delete_file(root: Path, storage_key: str) -> None:
    """删除磁盘文件。文件不存在视为成功（幂等）；其他失败仅告警，不抛异常。"""
    try:
        storage_path_for(root, storage_key).unlink(missing_ok=True)
    except OSError as exc:
        # 删除失败只会留下不可达的孤儿文件（DB 行已删），不影响请求结果
        logger.warning("document_storage.delete_failed key=%s err=%s", storage_key, exc)
