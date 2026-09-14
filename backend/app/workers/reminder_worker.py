"""Reminder Worker 独立进程入口。

用法：
    python -m app.workers.reminder_worker            # 无限轮询（Ctrl+C 优雅退出）
    python -m app.workers.reminder_worker --once     # 单次处理（测试/CI 用）
    python -m app.workers.reminder_worker --once --now 2026-08-20T10:00:00Z  # 固定时钟

设计决策：
- 不放 FastAPI lifespan：开发 reload 会启动多个 Worker 实例（重复轮询）；
- Worker 与 API 进程分离，数据库是唯一共享状态；
- 单次核心逻辑 process_due_reminders_once 可独立测试（Fake Clock，零 sleep）；
- 异常日志脱敏：错误消息移除数据库 URL 与 API Key 并截断，敏感内容不进日志。

退出码约定：
- 0：--once 成功完成（无论投递多少条）；
- 1：启动/配置失败（非法 --now、数据库不可用等）。
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.clock import FixedClock, SystemClock
from app.core.config import settings
from app.core.database import SessionLocal
from app.services.reminder_service import process_due_reminders_once

logger = logging.getLogger("reminder_worker")

# 日志消息长度上限：防止用户提供的长内容整段进入日志
MAX_LOG_MESSAGE_CHARS = 500


def sanitize_error_message(message: str) -> str:
    """脱敏错误消息：移除数据库 URL / API Key，并截断超长内容。

    Worker 的异常日志只允许出现脱敏后的消息——绝不包含连接串、密钥
    或用户提供的完整长文本（截断到 MAX_LOG_MESSAGE_CHARS）。
    """
    for secret in (settings.database_url, settings.llm_api_key):
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:MAX_LOG_MESSAGE_CHARS]


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SystemExit(f"非法时间格式（需要带时区）：{value!r}")
    return parsed.astimezone(timezone.utc)


def _safe_tick(db: Session) -> None:
    """执行一轮处理；任何异常都不让 Worker 循环崩溃，且日志经过脱敏。"""
    try:
        stats = process_due_reminders_once(
            db,
            SystemClock(),
            batch_size=settings.reminder_batch_size,
            lease_seconds=settings.reminder_lease_seconds,
            max_attempts=settings.reminder_max_attempts,
        )
        logger.info("reminder_worker.tick %s", stats)
    except Exception as exc:  # noqa: BLE001 - Worker 循环不允许崩溃退出
        logger.error(
            "reminder_worker.error type=%s message=%s",
            type(exc).__name__,
            sanitize_error_message(str(exc)),
        )


def run_forever(*, poll_interval: int) -> None:
    """无限轮询，支持 SIGINT/SIGTERM 优雅退出。"""
    stop = {"flag": False}

    def _handle(signum, frame):
        logger.info("reminder_worker.shutdown signal=%s", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    logger.info(
        "reminder_worker.start interval=%ss batch=%s lease=%ss max_attempts=%s",
        settings.reminder_poll_interval_seconds,
        settings.reminder_batch_size,
        settings.reminder_lease_seconds,
        settings.reminder_max_attempts,
    )
    while not stop["flag"]:
        db = SessionLocal()
        try:
            _safe_tick(db)
        finally:
            db.close()
        # 优雅退出时立即停止，不等待下一个 interval
        for _ in range(poll_interval):
            if stop["flag"]:
                break
            time.sleep(1)
    logger.info("reminder_worker.stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="JARVIS Reminder Worker")
    parser.add_argument("--once", action="store_true", help="只处理一次后退出（测试/CI 用）")
    parser.add_argument(
        "--now",
        default=None,
        help="固定当前时间（ISO 8601 带时区，如 2026-08-20T10:00:00Z）；仅测试/演示用",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        clock = FixedClock(_parse_iso(args.now)) if args.now else SystemClock()
        db = SessionLocal()
        try:
            stats = process_due_reminders_once(
                db,
                clock,
                batch_size=settings.reminder_batch_size,
                lease_seconds=settings.reminder_lease_seconds,
                max_attempts=settings.reminder_max_attempts,
            )
            print(f"reminder_worker.once stats={stats}")
            if not args.once:
                db.close()
                run_forever(poll_interval=settings.reminder_poll_interval_seconds)
        finally:
            db.close()
    except SystemExit:
        raise  # 配置错误（如非法 --now）：SystemExit 本身已是失败退出码
    except Exception as exc:  # noqa: BLE001 - 启动失败要给出干净错误与退出码 1
        logger.error(
            "reminder_worker.startup_failed type=%s message=%s",
            type(exc).__name__,
            sanitize_error_message(str(exc)),
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    sys.exit(main())
