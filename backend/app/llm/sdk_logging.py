"""openai SDK 日志隐私保护（Phase 10B Astra R2）。

定位：纵深防御（defence in depth）。

历史上的 openai SDK 版本在 DEBUG 级别会记录完整请求选项（``Request options: %s``，
含 messages：当前用户句子、Memory 解析 system prompt、structured-output 请求体）。
当前验证运行时 openai 3.24.0 的 ``openai.*`` 日志只含元数据（HTTP 方法、状态码、
request id、异常类型名、重试计数），不再记录请求体。本 Filter 仍然保留：它不依赖
某个 SDK 版本的日志内容，SDK 升级 / 降级后依然成立。SDK 自带的
SensitiveHeadersFilter 只遮蔽请求头里的 Authorization / api-key，并不遮蔽请求体。

本模块只做一件事：给 SDK 实际使用的 logger 挂一个 Filter，把每条记录改写为
固定文本（丢弃 args / exc_info / exc_text / stack_info），不在任何地方另存原内容。
logger 名、级别、行号等元数据保留，便于排障。

注意：Logger 级 Filter 只作用于「直接在该 logger 上产生」的记录，不作用于子 logger
传播上来的记录，因此必须挂在 SDK 实际产生记录的每个 logger 上，而不只是 "openai"。

传输层不在此处理。openai 3.24.0 的默认 HTTP 栈是 httpx2 2.13.1 → httpcore2 2.13.1
（不再是 httpx / httpcore；显式注入的 legacy ``httpx.Client`` 仍被 SDK 接受）：
- ``httpx2``（INFO）只记录方法、完整 URL 与状态行；
- ``httpcore2.connection`` / ``httpcore2.http11``（DEBUG）记录连接参数、
  Request 的 repr（仅方法）、异常 repr，以及**完整响应头**。

已接受残留（Phase 10B，Astra 裁定）：httpcore2 的 DEBUG 响应头日志不做脱敏。响应头
可能包含 request id、限流元数据、cookie 或供应商自定义字段，因此原始 DEBUG 日志
不得视为可公开 / 可分享的证据。测试（tests/test_phase10b_sdk_log_privacy.py）证明
该路径不含请求体、用户句子、prompt、响应体、API Key 或 Authorization 值。

Phase 9A 的 secure_library_logging() 只压制 legacy ``httpx`` / ``httpcore`` logger，
不覆盖 ``httpx2`` / ``httpcore2``。
"""

import logging

REDACTED_MESSAGE = "openai sdk log record redacted (JARVIS privacy)"

# chat completions / embeddings 调用链上产生日志的 SDK logger（预先 getLogger 即可
# 在 SDK 模块惰性导入前就挂上 Filter：getLogger 对同名返回同一对象）。
_SDK_LOGGER_NAMES = (
    "openai",
    "openai._base_client",
    "openai._response",
    "openai._legacy_response",
)


class SdkLogRedactionFilter(logging.Filter):
    """把 openai SDK 日志记录改写为固定文本；不丢弃记录，只丢弃内容。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = REDACTED_MESSAGE
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def protect_sdk_loggers() -> None:
    """幂等：为 SDK logger（固定名单 + 已存在的 openai.*）挂上脱敏 Filter。"""
    names = set(_SDK_LOGGER_NAMES)
    names.update(
        name
        for name in list(logging.Logger.manager.loggerDict)
        if name.startswith("openai.")
    )
    for name in sorted(names):
        sdk_logger = logging.getLogger(name)
        if not any(isinstance(f, SdkLogRedactionFilter) for f in sdk_logger.filters):
            sdk_logger.addFilter(SdkLogRedactionFilter())
