"""X-API-Key authentication for client requests."""

import hmac
import logging
from typing import Optional

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.core.tracing import current_trace, key_fingerprint

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

# auto_error=False: missing keys go through our handler, so the response stays {"error": ...}
_api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False,
                               description="API key issued for this service")


async def require_api_key(
    request: Request,
    api_key: Optional[str] = Security(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject the request unless X-API-Key matches a configured key (disabled if none set)."""
    if not settings.auth_enabled:
        return
    if api_key and any(
        hmac.compare_digest(api_key.encode(), key.get_secret_value().encode())
        for key in settings.api_keys
    ):
        trace = current_trace()
        if trace:  # which client / key was used, never the key itself
            trace.record["client"]["api_key_id"] = key_fingerprint(api_key)
        return
    client = request.client.host if request.client else "unknown"
    logger.warning("Rejected %s %s from %s: %s API key", request.method, request.url.path,
                   client, "invalid" if api_key else "missing")
    raise AuthenticationError()
