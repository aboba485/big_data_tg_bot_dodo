from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from html import escape
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, get_settings
from app.errors import ApplicationError
from app.planner.schemas import OutputFormat
from app.services import build_services

logger = logging.getLogger(__name__)


class ReportRequest(BaseModel):
    query: str = Field(min_length=3, max_length=4000)
    output_format: OutputFormat | None = None

    @field_validator("output_format")
    @classmethod
    def reject_sheets(cls, value: OutputFormat | None) -> OutputFormat | None:
        # Google Sheets export is tied to a linked Telegram user, which this transport lacks.
        if value == OutputFormat.SHEETS:
            raise ValueError("Формат sheets доступен только в Telegram-боте")
        return value


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        logging.basicConfig(
            level=current_settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        timeout = httpx.Timeout(current_settings.dodo_request_timeout_seconds)
        transport = (
            httpx.MockTransport(lambda _request: httpx.Response(500))
            if current_settings.dodo_mock_mode
            else None
        )
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            services = build_services(current_settings, client)
            application.state.services = services
            application.state.settings = current_settings
            try:
                yield
            finally:
                await services["google_drive"].aclose()

    application = FastAPI(
        title="Dodo IS Natural Reports",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def protect_compatibility_api(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        expected = current_settings.compatibility_api_key
        if not expected:
            return JSONResponse(
                status_code=503,
                content={"detail": "Compatibility API is disabled"},
            )
        provided = request.headers.get("x-api-key", "")
        if not provided or not secrets.compare_digest(provided, expected):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    @application.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
        request_id = request.headers.get("x-request-id", "")
        logger.exception("application_error request_id=%s code=%s", request_id, exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "request_id": request_id,
                "status": "error",
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            },
        )

    @application.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _static_page(
            "Dodo IS Reports Bot",
            """
            <p>Это служебное веб-приложение для Telegram-бота, который формирует отчёты
            на основе данных Dodo IS.</p>
            <p>Чтобы начать работу, перейдите в
            <a href="https://t.me/DodoReportsBot">Telegram-бот</a>.</p>
            <p><a href="/privacy">Политика конфиденциальности</a></p>
            """,
        )

    @application.get("/privacy", response_class=HTMLResponse)
    async def privacy() -> str:
        return _static_page(
            "Политика конфиденциальности",
            """
            <h2>Какие данные мы собираем</h2>
            <p>Бот обрабатывает только данные, необходимые для формирования отчётов:</p>
            <ul>
                <li>Ваш Telegram ID для идентификации пользователя</li>
                <li>Текст запросов для формирования отчётов</li>
                <li>При подключении Google Drive — адрес электронной почты и токены доступа
                    (хранятся в зашифрованном виде)</li>
            </ul>

            <h2>Как мы используем данные</h2>
            <p>Данные используются исключительно для:</p>
            <ul>
                <li>Формирования запрошенных отчётов</li>
                <li>Отправки отчётов в подключённый Google Drive</li>
                <li>Доставки запланированных (повторяющихся) отчётов</li>
            </ul>

            <h2>Хранение данных</h2>
            <p>Токены Google хранятся в зашифрованном виде. История запросов сохраняется
            для улучшения качества работы бота. Вы можете отключить Google Drive
            в любой момент командой /drive в боте.</p>

            <h2>Передача данных третьим лицам</h2>
            <p>Мы не передаём ваши данные третьим лицам, за исключением:</p>
            <ul>
                <li>Google — при использовании интеграции с Google Sheets</li>
                <li>Dodo IS API — для получения данных отчётов</li>
            </ul>

            <h2>Контакты</h2>
            <p>По вопросам конфиденциальности обращайтесь к администратору бота.</p>
            """,
        )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Google redirects the browser here without any header, so this route stays outside
    # the /api/ prefix. The single-use OAuth state is what authenticates the callback.
    @application.get("/google/oauth/callback", response_class=HTMLResponse)
    async def google_oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        if error or not code or not state:
            logger.info("google_oauth_callback_rejected reason=%s", "error" if error else "missing")
            return HTMLResponse(
                _oauth_page(
                    "Подключение не завершено",
                    "Google не передал код авторизации. Начните подключение заново в боте.",
                ),
                status_code=400,
            )
        try:
            _telegram_id, email = await request.app.state.services["google_drive"].complete_link(
                code, state
            )
        except ApplicationError as exc:
            logger.warning("google_oauth_callback_failed code=%s", exc.code)
            return HTMLResponse(
                _oauth_page("Не удалось подключить Google Drive", exc.message),
                status_code=400,
            )
        return HTMLResponse(
            _oauth_page(
                "Google Drive подключён",
                f"Аккаунт {email} подключён. Вернитесь в Telegram — эту вкладку можно закрыть.",
            )
        )

    @application.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        services = request.app.state.services
        stats = services["repository"].stats()
        ok = services["database"].ping() and bool(stats["indexed_operation_count"])
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "not_ready", "checks": stats},
        )

    @application.get("/api/documentation/stats")
    async def documentation_stats(request: Request) -> dict[str, Any]:
        return request.app.state.services["repository"].stats()

    @application.get("/api/documentation/search")
    async def documentation_search(
        request: Request, q: str = Query(min_length=1, max_length=500)
    ) -> list[dict[str, Any]]:
        candidates = await request.app.state.services["retrieval"].search_endpoints(q)
        return [candidate.model_dump() for candidate in candidates]

    @application.get("/api/metrics")
    async def metrics(request: Request) -> dict[str, Any]:
        return request.app.state.services["metrics"].all()

    @application.post("/api/reports")
    async def reports(payload: ReportRequest, request: Request) -> dict[str, Any]:
        return await request.app.state.services["orchestrator"].create_report(
            payload.query, payload.output_format
        )

    @application.get("/api/reports/{report_id}/download")
    async def download(report_id: str, request: Request) -> FileResponse:
        if not report_id.isalnum() or len(report_id) != 32:
            return await http_exception_handler(request, _not_found())
        found = request.app.state.services["files"].get(report_id)
        if not found:
            return await http_exception_handler(request, _not_found())
        path, media_type = found
        reports_root = request.app.state.settings.reports_directory.resolve()
        try:
            path.resolve().relative_to(reports_root)
        except ValueError:
            return await http_exception_handler(request, _not_found())
        if not path.is_file():
            return await http_exception_handler(request, _not_found())
        return FileResponse(path, media_type=media_type, filename=path.name)

    return application


def _oauth_page(title: str, message: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title></head>"
        '<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;'
        'padding:0 1rem;line-height:1.5">'
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p></body></html>"
    )


def _static_page(title: str, body_html: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title></head>"
        '<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;'
        'padding:0 1rem;line-height:1.6">'
        f"<h1>{escape(title)}</h1>{body_html}</body></html>"
    )


def _not_found() -> Any:
    from fastapi import HTTPException

    return HTTPException(status_code=404, detail="Отчёт не найден")


app = create_app()
