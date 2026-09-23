"""Keep the global Codex client version used for catalog refresh and upstream calls."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

CODEX_CLI_LATEST_URL = "https://registry.npmjs.org/@openai/codex/latest"
_STABLE_VERSION = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")


def parse_codex_cli_version(payload: object) -> str | None:
    """Accept a stable ``major.minor.patch`` from the npm ``latest`` document."""
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if not isinstance(version, str):
        return None
    version = version.strip()
    if version.startswith("v"):
        version = version[1:]
    if not _STABLE_VERSION.fullmatch(version):
        return None
    return version


async def refresh_codex_client_version() -> str:
    """Replace the global Codex client version when npm publishes a newer stable CLI.

    Model refresh and upstream calls both read ``settings.codex_client_version``.
    A failed lookup keeps the current value. The configured default is only the
    seed used until the first successful refresh.
    """
    from app.http_client import shared_client

    previous = settings.codex_client_version
    try:
        response = await (await shared_client()).get(
            CODEX_CLI_LATEST_URL,
            headers={"Accept": "application/json", "User-Agent": "transfer-station"},
            timeout=10,
        )
    except Exception:
        logger.warning("codex_client_version_refresh_failed")
        return previous
    if response.status_code != 200:
        logger.warning("codex_client_version_refresh_failed status=%s", response.status_code)
        return previous
    try:
        payload: Any = response.json()
    except Exception:
        logger.warning("codex_client_version_refresh_rejected")
        return previous
    version = parse_codex_cli_version(payload)
    if version is None:
        logger.warning("codex_client_version_refresh_rejected")
        return previous
    if version != previous:
        settings.codex_client_version = version
        logger.info("codex_client_version_updated from=%s to=%s", previous, version)
    return settings.codex_client_version
