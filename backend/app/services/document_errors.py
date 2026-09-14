"""文档领域错误（Phase 4A）。

所有错误携带稳定错误码；HTTP 状态码由 main.py 统一映射。文案全部 sanitized：
不含绝对路径、堆栈、API Key、文件内容片段或原始异常细节。

错误码一览（对客户端稳定，前端/测试依赖）：
- UNSUPPORTED_FILE_TYPE   415  扩展名不在白名单 / .docm
- FILE_TOO_LARGE          413  超过 document_max_file_size_mb
- INVALID_FILE_CONTENT    422  Content-Type 与扩展名冲突 / 文件头与扩展名不符（伪装）/
                                 损坏文件（非法 zip / 非法 pdf）
- DOCX_ZIP_BOMB_DETECTED  422  ZIP bomb 防御（条目数 / 解压总量 / 压缩比超限）
- DOCUMENT_NOT_FOUND      404  文档不存在
- INVALID_DOCUMENT_STATE  409  状态不允许的操作（如对非 READY 文档取 chunks）
- DOCUMENT_PROCESSING_FAILED  500  解析/切块阶段失败（记录在文档的 FAILED 状态上，
                                 上传请求本身仍成功返回 201 + status=FAILED）
"""

# 错误码 → HTTP 状态（main.py 的全局 handler 使用；前端依赖这些状态码）
DOCUMENT_ERROR_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 422,
    "UNSUPPORTED_FILE_TYPE": 415,
    "FILE_TOO_LARGE": 413,
    "INVALID_FILE_CONTENT": 422,
    "DOCX_ZIP_BOMB_DETECTED": 422,
    "DOCUMENT_NOT_FOUND": 404,
    "INVALID_DOCUMENT_STATE": 409,
    "DOCUMENT_PROCESSING_FAILED": 500,
}


class DocumentError(Exception):
    """文档领域错误基类。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class DocumentValidationError(DocumentError):
    """请求本身非法（如缺少文件名）→ 422 VALIDATION_ERROR（与 Pydantic 校验一致）。"""

    def __init__(self, message: str):
        super().__init__("VALIDATION_ERROR", message)


class UnsupportedFileTypeError(DocumentError):
    def __init__(self, message: str):
        super().__init__("UNSUPPORTED_FILE_TYPE", message)


class FileTooLargeError(DocumentError):
    def __init__(self, message: str):
        super().__init__("FILE_TOO_LARGE", message)


class InvalidFileContentError(DocumentError):
    def __init__(self, message: str):
        super().__init__("INVALID_FILE_CONTENT", message)


class DocxZipBombError(DocumentError):
    def __init__(self, message: str):
        super().__init__("DOCX_ZIP_BOMB_DETECTED", message)


class DocumentNotFoundError(DocumentError):
    def __init__(self, document_id: int):
        super().__init__("DOCUMENT_NOT_FOUND", f"Document {document_id} does not exist")


class InvalidDocumentStateError(DocumentError):
    def __init__(self, document_id: int, expected: str, actual: str):
        super().__init__(
            "INVALID_DOCUMENT_STATE",
            f"Document {document_id} is {actual}, expected {expected}",
        )


class DocumentProcessingError(DocumentError):
    """解析/切块阶段失败。上传端点内部捕获后转为 FAILED 状态（sanitized 简述）。"""

    def __init__(self, message: str):
        super().__init__("DOCUMENT_PROCESSING_FAILED", message)
