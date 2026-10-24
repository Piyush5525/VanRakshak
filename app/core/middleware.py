import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("vananetra")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(message)s")
    for noisy in ("httpx", "httpcore"):  # request URLs can contain API keys
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Correlation IDs, structured access logs and a per-client sliding-window rate limit.

    Unhandled errors return a generic body so internals are not leaked.
    """

    def __init__(self, app, per_minute: int):
        super().__init__(app)
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def _limited(self, key: str) -> bool:
        if self.per_minute <= 0:
            return False
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return True
            q.append(now)
        return False

    async def dispatch(self, request: Request, call_next):
        cid = request.headers.get("x-correlation-id", "")[:64] or str(uuid.uuid4())
        request.state.correlation_id = cid
        client = request.client.host if request.client else "unknown"
        started = time.perf_counter()
        if request.url.path.startswith("/api/") and self._limited(client):
            response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        else:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 - log with correlation id, return generic body
                logger.exception(json.dumps({"event": "unhandled_error", "correlation_id": cid, "path": request.url.path}))
                response = JSONResponse({"detail": "Internal server error", "correlation_id": cid}, status_code=500)
        response.headers["X-Correlation-ID"] = cid
        logger.info(json.dumps({"event": "request", "correlation_id": cid, "method": request.method, "path": request.url.path, "status": response.status_code, "ms": round((time.perf_counter() - started) * 1000, 1)}))
        return response
