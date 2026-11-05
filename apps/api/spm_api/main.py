"""Application factory and privacy-preserving health endpoints."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from spm_api import __version__
from spm_api.auth_routes import router as auth_router
from spm_api.cash_routes import router as cash_router
from spm_api.config import Settings
from spm_api.db import check_database, make_engine
from spm_api.errors import ApiError, Error, Status, error_response
from spm_api.finance import FinanceError
from spm_api.master_routes import router as master_router
from spm_api.trade_routes import router as trade_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = make_engine(settings.database_path)
        try:
            yield
        finally:
            app.state.engine.dispose()

    app = FastAPI(
        title="Stock Portfolio Manager 2.0 API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.app_env == "production" else "/openapi.json",
    )
    app.state.settings = settings

    @app.middleware("http")
    async def privacy_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def application_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(FinanceError)
    async def financial_error(request: Request, exc: FinanceError) -> JSONResponse:
        status = (
            409 if exc.code in {"INSUFFICIENT_SHARES", "NEEDS_REVIEW", "DUPLICATE_FACT"} else 422
        )
        return error_response(request, status, exc.code, str(exc))

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        return error_response(request, 409, "CONFLICT", "The change violates a data constraint")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return error_response(request, 503, "DATABASE_UNAVAILABLE", "The database is unavailable")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "REQUEST_ERROR"
        return error_response(request, exc.status_code, code, "Request could not be processed")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # ValidationError.input can contain passwords or private data; never serialize it.
        return error_response(request, 422, "VALIDATION_ERROR", "Invalid request parameters")

    @app.get("/api/v1/health/live", operation_id="health_live", tags=["Health"])
    def health_live() -> Status:
        return Status(status="ok")

    @app.get(
        "/api/v1/health/ready",
        operation_id="health_ready",
        tags=["Health"],
        response_model=Status,
        responses={503: {"model": Error}},
    )
    def health_ready(request: Request) -> Status | JSONResponse:
        try:
            check_database(request.app.state.engine, settings)
        except (SQLAlchemyError, RuntimeError, OSError):
            return error_response(request, 503, "NOT_READY", "Database is not ready")
        return Status(status="ready")

    app.include_router(auth_router)
    app.include_router(master_router)
    app.include_router(trade_router)
    app.include_router(cash_router)
    return app


app = create_app()
