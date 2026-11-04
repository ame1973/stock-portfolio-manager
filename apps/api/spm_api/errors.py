"""Shared HTTP models and safe application errors."""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Status(Model):
    status: str


class Success(Model):
    ok: bool = True
    message: str = "OK"


class Error(Model):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = {}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    response = JSONResponse(
        status_code=status,
        content=Error(code=code, message=message, request_id=request.state.request_id).model_dump(),
    )
    if status in {429, 503}:
        response.headers["Retry-After"] = "60" if status == 429 else "1"
    return response
