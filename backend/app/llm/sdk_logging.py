"""openai SDK 日志隐私保护（Phase 10B Astra R2）。

已安装的 openai SDK 在 DEBUG 级别会记录完整请求选项（``Request options: %s``，
含 messages：当前用户句子、Memory 解析 system prompt、structured-output 请求体），
以及 HTTP 响应头与带 traceback 的异常。SDK 自带的 SensitiveHeadersFilter 只遮蔽
请求头里的 Authorization / api-key，并不遮蔽请求体。

本模块只做一件事：给 SDK 实际使用的 logger 挂一个 Filter，把每条记录改写为
固定文本（丢弃 args / exc_info / stack_info），不在任何地方另存原内容。
logger 名、级别、行号等元数据保留，便于排障。

注意：Logger 级 Filter 只作用于「直接在该 logger 上产生」的记录，不作用于子 logger
传播上来的记录，因此必须挂在 SDK 实际产生记录的每个 logger 上，而不只是 "openai"。

httpx / httpcore 不在此处理：httpx 只记录方法、URL 与状态行；httpcore 的 trace 只
记录 Request/Response 的 repr 与响应头，均不含请求体或 Authorization。
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
