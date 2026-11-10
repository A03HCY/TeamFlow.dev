"""统一的业务异常：handler 会把它渲染成 ``{"error": ..., "code": ...}``。"""
from __future__ import annotations


class ApiError(Exception):
    status = 400
    code: str | None = None

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
