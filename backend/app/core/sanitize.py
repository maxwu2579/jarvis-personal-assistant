"""通用错误消息 / 日志 / 输入字段脱敏（Phase 6A）。

独立于 workers/reminder_worker.sanitize_error_message（Phase 3 封存代码，
不修改）；本模块是其通用化版本：
- 覆盖全部已知密钥配置（database_url / llm_api_key / embedding_api_key——
  Phase 5C 审计发现 Worker 脱敏清单未含 embedding_api_key，此处一并覆盖）；
- 提供输入字段 deny 词表：payload / 日志中的字段名命中即拒绝写入与落盘，
  敏感数据（Key、密码、连接串、Token）绝不持久化。

绝不包含：堆栈、绝对路径、API Key、数据库 URL、完整敏感输入。
"""

from app.core.config import settings

# 日志消息长度上限：防止用户提供的长内容整段进入日志（沿 Worker 惯例）
MAX_SANITIZED_MESSAGE_CHARS = 500

# 字段名 deny 词表：输入 payload / 事件中命中即拒绝（registry 白名单之外的
# 第二层防御——即使白名单误配放行，敏感字段名也会被拦截）
DENY_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "auth_token",
        "database_url",
        "connection_string",
        "dsn",
    }
)

# 完整值替换清单：日志/错误消息中的已知连接串与密钥整串替换
_SECRET_VALUES: tuple[str, ...] = (
    settings.database_url,
    settings.llm_api_key,
    settings.embedding_api_key,
)


def sanitize_error_message(message: str, *, max_chars: int = MAX_SANITIZED_MESSAGE_CHARS) -> str:
    """脱敏错误消息：移除数据库 URL / API Key，并截断超长内容。

    Worker 与服务的异常日志只允许出现脱敏后的消息——绝不包含连接串、
    密钥或用户提供的完整长文本。
    """
    for secret in _SECRET_VALUES:
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:max_chars]


def is_denied_field_name(field_name: str) -> bool:
    """字段名是否命中 deny 词表（大小写不敏感）。

    registry 校验 payload 字段时调用：命中即拒绝，敏感字段绝不进入数据库。
    """
    lowered = (field_name or "").strip().lower()
    return lowered in DENY_FIELD_NAMES or lowered.endswith("_key") or lowered.endswith("_token")


def sanitize_result_value(value, *, depth: int = 0, max_chars: int = MAX_SANITIZED_MESSAGE_CHARS):
    """递归脱敏 handler 返回结果（Phase 6B）：敏感字段名 → "<redacted>"，
    字符串截断。Worker 持久化 result_text 前必须经过本函数——handler
    输出中的 Key/Token/长文本绝不原样落盘。"""
    if depth > 10:
        return "<redacted>"
    if isinstance(value, dict):
        return {
            (key if not is_denied_field_name(key) else "<redacted>"):
            (
                "<redacted>"  # 敏感字段整字段替换（值一并脱敏，绝不落盘）
                if is_denied_field_name(key)
                else sanitize_result_value(item, depth=depth + 1, max_chars=max_chars)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize_result_value(item, depth=depth + 1, max_chars=max_chars)
            for item in value
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars]
    return str(value)[:max_chars]


def has_denied_field_name(value, *, depth: int = 0) -> bool:
    """递归检测输入 JSON 中是否存在敏感字段名（Phase 6C 输入防线）。

    6A 只校验 payload 顶层字段名；步骤输入可能嵌套（schema 校验前）——
    本函数递归遍历 dict/list，任何层级命中 deny 词表即返回 True
    （fail-closed：绝不静默剥离，直接拒绝执行并报 EXEC_SCHEMA_INVALID）。
    """
    if depth > 20:
        return True  # 超出递归上限视为不可信，拒绝（fail-closed）
    if isinstance(value, dict):
        for key, item in value.items():
            if is_denied_field_name(key):
                return True
            if has_denied_field_name(item, depth=depth + 1):
                return True
        return False
    if isinstance(value, list):
        return any(has_denied_field_name(item, depth=depth + 1) for item in value)
    return False
