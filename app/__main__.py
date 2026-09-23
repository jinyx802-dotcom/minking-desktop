from __future__ import annotations

import logging

import uvicorn

from app.config import settings


def _configure_application_logging() -> None:
    level = getattr(logging, settings.log_level.strip().upper(), logging.INFO)
    app_logger = logging.getLogger("transfer_station.errors")
    app_logger.setLevel(level)
    app_logger.propagate = False
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        app_logger.addHandler(handler)
    for handler in app_logger.handlers:
        handler.setLevel(level)


def main() -> None:
    _configure_application_logging()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.strip().lower(),
        access_log=settings.access_log_enabled,
    )


if __name__ == "__main__":
    main()
