"""HTTP 基础类型：不依赖任何业务模块，供 run.py 与 api_handlers 共用。

为什么单独一个模块：
  `run.py` 是被直接执行的（`python run.py`），此时它的模块名是 `__main__`。
  若 api_handlers 里写 `from run import Response`，Python 会**再导入一份** `run` 模块，
  于是出现两个不同的 `Response` 类 —— `isinstance(result, Response)` 恒为 False，
  接口会把 Response 对象序列化成字符串返回（表现为「登录成功但拿不到 cookie」）。
  把共享类型放在这里即可彻底避免。
"""

from __future__ import annotations

import json
from typing import Any

MAX_JSON_BODY = 2 * 1024 * 1024
MAX_BODY = 2 * 1024 * 1024 * 1024          # 2 GiB（存档备份可能很大）
MAX_UPLOAD = 2 * 1024 * 1024 * 1024


class ApiError(Exception):
    """业务层可预期的错误，会被转换为对应 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400,
                 *, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


class Response:
    """HTTP 响应封装。body 为 dict/list 时按 JSON 编码。"""

    def __init__(self, body: Any = None, *, status: int = 200,
                 content_type: str = "application/json; charset=utf-8",
                 headers: dict[str, str] | None = None,
                 raw: bytes | None = None) -> None:
        self.status = status
        self.content_type = content_type
        self.headers = headers or {}
        if raw is not None:
            self.body = raw
        elif content_type.startswith("application/json"):
            self.body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(body, bytes):
            self.body = body
        else:
            self.body = str(body if body is not None else "").encode("utf-8")


# 无需登录即可访问的端点（健康检查、登录、登录态查询）
PUBLIC_PATHS = ("/api/health", "/api/login", "/api/auth/state")
