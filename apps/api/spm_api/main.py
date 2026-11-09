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
from spm_api.action_routes import router as action_router
from spm_api.auth_routes import router as auth_router
from spm_api.basis_routes import router as basis_router
from spm_api.cash_routes import router as cash_router
from spm_api.cited_routes import router as cited_router
from spm_api.config import Settings
from spm_api.db import check_database, make_engine
from spm_api.dividend_basis_service import BasisError
from spm_api.errors import ApiError, Error, Status, error_response
from spm_api.finance import FinanceError
from spm_api.master_routes import router as master_router
from spm_api.observation_routes import router as observation_router
from spm_api.profile_routes import router as profile_router
from spm_api.provider_routes import router as provider_router
from spm_api.providers.credentials import CredentialError
from spm_api.providers.profiles import ProfileError
from spm_api.schedule_routes import router as schedule_router
from spm_api.schedule_service import ScheduleError
from spm_api.sync_routes import router as sync_router
from spm_api.trade_routes import router as trade_router
from spm_api.transfer_routes import router as transfer_router


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

    @app.exception_handler(CredentialError)
    async def credential_error(request: Request, exc: CredentialError) -> JSONResponse:
        return error_response(
            request,
            503,
            "PROVIDER_CREDENTIAL_STORAGE_UNAVAILABLE",
            "Private provider credential storage is unavailable",
        )

    @app.exception_handler(ProfileError)
    async def profile_error(request: Request, exc: ProfileError) -> JSONResponse:
        code = str(exc)
        if code == "PROFILE_NOT_FOUND":
            return error_response(
                request, 404, "NOT_FOUND", "Managed provider definition not found"
            )
        if code == "PROVIDER_PROFILE_UNAVAILABLE":
            return error_response(
                request, 503, code, "The stored provider definition cannot be read safely"
            )
        if code.startswith("INVALID_"):
            return error_response(
                request, 422, "INVALID_PROVIDER_PROFILE", "Invalid provider definition"
            )
        return error_response(
            request,
            409,
            "PROVIDER_PROFILE_CHANGE_REJECTED",
            "The provider definition change could not be accepted",
        )

    @app.exception_handler(ScheduleError)
    async def schedule_error(request: Request, exc: ScheduleError) -> JSONResponse:
        code = str(exc)
        if code == "SCHEDULE_NOT_FOUND":
            return error_response(request, 404, "NOT_FOUND", "Source schedule not found")
        if code in {
            "SCHEDULE_POLICY_UNAVAILABLE",
            "SCHEDULE_SLOT_UNAVAILABLE",
            "SCHEDULE_RECORD_UNAVAILABLE",
        }:
            return error_response(
                request,
                503,
                "SOURCE_SCHEDULE_UNAVAILABLE",
                "The stored source schedule cannot be read safely",
            )
        if code == "SCHEDULE_SECURITY_SCOPE":
            return error_response(
                request,
                422,
                "INVALID_SOURCE_SCHEDULE_SCOPE",
                "The schedule securities do not match the requested market",
            )
        return error_response(
            request,
            409,
            "SOURCE_SCHEDULE_CHANGE_REJECTED",
            "The source schedule change could not be accepted",
        )

    @app.exception_handler(BasisError)
    async def basis_error(request: Request, exc: BasisError) -> JSONResponse:
        code = str(exc)
        if code == "BASIS_ASSERTION_NOT_FOUND":
            return error_response(request, 404, "NOT_FOUND", "Basis declaration not found")
        if code == "BASIS_OWNER_UNAVAILABLE":
            return error_response(request, 401, "UNAUTHORIZED", "Authentication is required")
        if code in {"INVALID_BASIS_SCOPE", "INVALID_BASIS_ASSERTION"}:
            return error_response(request, 422, code, "The basis declaration is invalid")
        if code in {"BASIS_SOURCE_CHANGED", "BASIS_ACTIONS_CHANGED", "BASIS_ASSERTION_CONFLICT"}:
            return error_response(request, 409, code, "The basis declaration no longer matches")
        return error_response(
            request,
            503,
            "BASIS_ASSERTION_UNAVAILABLE",
            "The basis declaration cannot be read safely",
        )

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
    app.include_router(transfer_router)
    app.include_router(action_router)
    app.include_router(sync_router)
    app.include_router(profile_router)
    app.include_router(provider_router)
    app.include_router(observation_router)
    app.include_router(cited_router)
    app.include_router(basis_router)
    app.include_router(schedule_router)
    return app


app = create_app()
