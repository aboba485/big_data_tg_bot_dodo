from app.bot.handlers.common import router as common_router
from app.bot.handlers.google_drive import router as google_drive_router
from app.bot.handlers.reports import router as reports_router
from app.bot.handlers.weekly import router as weekly_router

__all__ = ["common_router", "google_drive_router", "reports_router", "weekly_router"]
