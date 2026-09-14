"""Execution 真实业务工具 handler（Phase 7.1）。

每个工具一个模块，handler 均为注册表内静态引用的白名单函数
（见 app/services/execution_registry.py 的 EXECUTION_TOOLS / EXECUTION_TYPES）：
- 模块内自包含输入/输出 schema（pydantic extra="forbid"）；
- 模块级 _session_factory / _clock 注入点（生产默认
  app.core.database.SessionLocal / SystemClock，测试 monkeypatch 替换）；
- 绝不包含字符串 import、eval、exec、反射调用、任意 shell/命令执行；
- 业务副作用完全复用既有服务（reminder_service._deliver /
  RetrievalIndexService.reindex_document），不复制实现、不绕过
  Worker / lease / step / event / confirmation / terminal 裁决。
"""
