"""可注入 Clock：测试不依赖真实当前时间。

所有「当前时间」必须通过 Clock 获取；测试用 FixedClock 注入固定时刻。
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """当前 UTC 时刻（aware）。"""
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """固定时刻时钟，仅用于测试。"""

    def __init__(self, fixed: datetime):
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=timezone.utc)
        self.fixed = fixed.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.fixed


def get_clock() -> Clock:
    """FastAPI 依赖：生产用系统时钟，测试可覆盖。"""
    return SystemClock()
