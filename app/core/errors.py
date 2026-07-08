from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError


class ApiError(Exception):
    def __init__(self, status_code: int, error: str, message: str) -> None:
        self.status_code = status_code
        self.error = error
        self.message = message


def conflict(error: str, message: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, error, message)


def api_error_response(error: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "message": message},
    )


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return api_error_response(exc.error, exc.message, exc.status_code)


async def integrity_error_handler(_request: Request, _exc: IntegrityError) -> JSONResponse:
    return api_error_response(
        "integrity_conflict",
        "Request conflicts with existing or linked records.",
        status.HTTP_409_CONFLICT,
    )
