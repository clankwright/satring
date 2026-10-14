import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import init_db, async_session
from app.models import Category
from app.health import start_health_task, stop_health_task
from app.submissions import start_publish_task, stop_publish_task
from app.usage import record_hit, record_details, record_agent, start_flush_task, stop_flush_task

# SECURITY: Rate limiter to prevent abuse and DoS. Applied per-endpoint in route files.
limiter = Limiter(key_func=get_remote_address)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """SECURITY: Add CSP, HSTS, Referrer-Policy, and other hardening headers."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Content-Security-Policy: restrict resource origins.
        # 'unsafe-inline' is required because the app uses inline <script>/<style>
        # blocks and onclick handlers; still a big win because it blocks unknown origins.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


class UsageTrackingMiddleware(BaseHTTPMiddleware):
    """Record endpoint hits for usage analytics. Tracks agent traffic and discovery signals."""

    # 404s on these prefixes are tracked as "miss" source (agent demand signals)
    _DISCOVERY_PREFIXES = ("/.well-known/", "/api/")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "")
        path = request.url.path

        # Always record agent class (regardless of status code)
        record_agent(user_agent, client_ip)

        if response.status_code >= 500:
            return response

        # Track 404s on discovery paths as "miss" source
        if response.status_code == 404:
            if any(path.startswith(p) for p in self._DISCOVERY_PREFIXES):
                record_hit(path, "miss", client_ip)
            return response

        source = "api" if path.startswith("/api/") else "web"
        record_hit(path, source, client_ip)
        record_details(path, dict(request.query_params), client_ip)
        return response


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """SECURITY: Reject cross-origin POST/PUT/DELETE/PATCH requests.
    Prevents CSRF by verifying the Origin header matches BASE_URL.
    This app has no session cookies so CSRF risk is limited, but this
    is a low-cost defense-in-depth measure."""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin:
                allowed = urlparse(settings.BASE_URL).netloc
                actual = urlparse(origin).netloc
                if actual != allowed:
                    if request.url.path.startswith("/api/"):
                        return JSONResponse(
                            {"detail": "Cross-origin request blocked"},
                            status_code=403,
                        )
                    return HTMLResponse("Cross-origin request blocked", status_code=403)
        return await call_next(request)

class ApiCorsMiddleware(BaseHTTPMiddleware):
    """Permissive CORS for /api/ routes so browser-based agents can read x402/L402
    payment challenges and send payment headers cross-origin.

    Scoped to /api/. Advertises GET only, so cross-origin writes stay blocked by
    OriginCheckMiddleware: CORS and the CSRF defense agree. Web (HTML) routes are
    untouched, and server-side clients (the main audience) are unaffected since
    CORS is browser-enforced only. This is not an access control: free endpoints
    already gate scraping via thin summaries + per-IP quota + the payment gate."""

    # Request headers a paying client sends (x402 + L402/MPP).
    _ALLOW_HEADERS = "Authorization, Content-Type, PAYMENT-SIGNATURE"
    # Response headers a client must read; none are CORS-safelisted by default.
    _EXPOSE_HEADERS = "PAYMENT-REQUIRED, PAYMENT-RESPONSE, WWW-Authenticate"

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        # Answer the CORS preflight here; API routes define no OPTIONS handler.
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Headers": self._ALLOW_HEADERS,
                    "Access-Control-Max-Age": "600",
                },
            )

        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = self._EXPOSE_HEADERS
        return response


SEED_CATEGORIES = [
    ("ai/ml", "ai-ml", "Machine learning and AI inference APIs"),
    ("data", "data", "Data feeds, aggregation, and analytics"),
    ("finance", "finance", "Financial data, trading, and payment APIs"),
    ("identity", "identity", "KYC, authentication, and verification"),
    ("media", "media", "Image, video, and audio processing"),
    ("search", "search", "Web search, indexing, and discovery"),
    ("social", "social", "Social networks, communications, and notification APIs"),
    ("storage", "storage", "File storage and content delivery"),
    ("tools", "tools", "Developer tools, utilities, and infrastructure"),
]


async def seed_categories():
    async with async_session() as db:
        result = await db.execute(select(Category).limit(1))
        if result.scalars().first() is not None:
            return
        for name, slug, description in SEED_CATEGORIES:
            db.add(Category(name=name, slug=slug, description=description))
        await db.commit()


logger = logging.getLogger("satring")

# File logging: all satring.* loggers write to logs/satring.log
_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_handler = RotatingFileHandler(
    os.path.join(_log_dir, "satring.log"),
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_file_handler.setLevel(logging.INFO)
logging.getLogger("satring").addHandler(_file_handler)
logging.getLogger("satring").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # SECURITY: Refuse to start without an explicit AUTH_ROOT_KEY.
    # Operators must set a real key for production or "test-mode" for development.
    if not settings.AUTH_ROOT_KEY:
        raise RuntimeError(
            "AUTH_ROOT_KEY is not set. Set it to a secure random key for production, "
            "or 'test-mode' to explicitly disable payment gates for development."
        )
    if settings.AUTH_ROOT_KEY == "test-mode":
        logger.warning("AUTH_ROOT_KEY is 'test-mode' — payment gates are bypassed.")
    await init_db()
    await seed_categories()
    start_flush_task()
    start_health_task()
    start_publish_task()
    yield
    await stop_publish_task()
    await stop_health_task()
    await stop_flush_task()


app = FastAPI(title="satring", description="Curated paid API directory for AI agents. L402, x402, and MPP services with health monitoring, human/agent ratings, and MCP integration.", lifespan=lifespan, docs_url=None)


def _custom_openapi():
    """Extend the generated OpenAPI schema with MPP discovery metadata."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    # Security schemes for payment protocols
    schema.setdefault("components", {})["securitySchemes"] = {
        "L402": {
            "type": "http",
            "scheme": "L402",
            "description": "L402 Lightning payment: Authorization: L402 <macaroon>:<preimage>",
        },
        "MPP": {
            "type": "http",
            "scheme": "Payment",
            "description": "MPP Lightning payment: Authorization: Payment <base64url-credential>",
        },
        "x402": {
            "type": "apiKey",
            "in": "header",
            "name": "PAYMENT-SIGNATURE",
            "description": "x402 USDC payment: base64-encoded payment signature",
        },
    }
    # x-service-info (draft-payment-discovery-00)
    schema["x-service-info"] = {
        "categories": ["search", "data"],
        "docs": {
            "homepage": "https://satring.com/docs",
            "llms": "https://satring.com/llms.txt",
            "apiReference": "https://satring.com/docs",
        },
    }
    # info.x-guidance for agent-readable instructions (per MPP discovery spec)
    schema["info"]["x-guidance"] = (
        "Satring is a curated paid API directory. "
        "Free endpoints (list, search, detail) return thin summaries (name, protocol, price, rating) "
        "with a daily quota of 5 results per IP. Summaries do NOT include service URLs, descriptions, "
        "or payment config. To get full service metadata including endpoint URLs, pay via bulk export "
        "or exhaust the free quota to unlock full details. "
        "Payment methods: L402 (Authorization: L402 <macaroon>:<preimage>), "
        "MPP (Authorization: Payment <base64url-credential>), "
        "or x402 (PAYMENT-SIGNATURE header). "
        "Hit any paid endpoint without auth to receive a 402 with payment challenges for all supported protocols. "
        "Endpoints marked with x-payment-info show the cost in USD."
    )
    # x-discovery for ownership proofs (per MPP discovery spec)
    schema["x-discovery"] = {
        "ownershipProofs": [],
    }
    # Remove non-API routes (web UI, static pages) from the spec
    paths = schema.get("paths", {})
    api_paths = {k: v for k, v in paths.items() if k.startswith("/api/")}
    schema["paths"] = api_paths

    # Add security references and 402 responses to paid endpoints,
    # and ensure all endpoints have an auth mode for MPP discovery.
    payment_security = [{"L402": []}, {"MPP": []}, {"x402": []}]
    for path_ops in api_paths.values():
        for op in path_ops.values():
            if not isinstance(op, dict):
                continue
            pi = op.get("x-payment-info")
            if not pi:
                continue
            # Only add security to actually paid endpoints (price != "0")
            if pi.get("price", "0") != "0":
                op.setdefault("security", payment_security)
                op.setdefault("responses", {})["402"] = {
                    "description": "Payment Required"
                }
            else:
                # Free endpoints: reference a security scheme so the
                # mppscan validator recognizes an auth mode declaration.
                op.setdefault("security", [{"x402": []}])
            responses = op.get("responses", {})
            # Copy 201 schema to 200 if 200 is missing (for POST endpoints)
            if "201" in responses and "200" not in responses:
                responses["200"] = responses["201"]

    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi
app.state.limiter = limiter
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    # Parse window from slowapi detail (e.g. "6 per 1 minute")
    retry_after = 60  # default fallback
    detail = str(exc.detail) if exc.detail else ""
    if "second" in detail:
        retry_after = 1
    elif "minute" in detail:
        retry_after = 60
    elif "hour" in detail:
        retry_after = 3600
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {detail}"},
        headers={"Retry-After": str(retry_after)},
    )

app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(UsageTrackingMiddleware)
app.add_middleware(ApiCorsMiddleware)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui():
    return HTMLResponse("""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>satring | API Docs</title>
<link rel="icon" type="image/png" href="/static/img/satring-logo-trans-bg.png">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<link rel="stylesheet" href="/static/css/theme.css">
<style>body { transition: opacity 0.15s; }</style>
</head><body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
<script>
SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
  plugins: [SwaggerUIBundle.plugins.DownloadUrl],
  layout: "StandaloneLayout",
  syntaxHighlight: { theme: "monokai" },
  deepLinking: true,
});
document.body.style.opacity = "1";
</script>
</body></html>""")

from pathlib import Path

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

from app.routes.web import router as web_router   # noqa: E402
from app.routes.api import router as api_router    # noqa: E402

app.include_router(web_router)
app.include_router(api_router, prefix="/api/v1")
# Static mounts AFTER routers so dynamic well-known routes take priority
app.mount("/.well-known", StaticFiles(directory=Path(__file__).parent / ".well-known"), name="well-known")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# --- API path redirects for common agent 404s ---
from starlette.responses import RedirectResponse as StarletteRedirect


@app.api_route("/api/services{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def redirect_api_services(request: Request, path: str):
    """Redirect /api/services... -> /api/v1/services... (agents expect no v1 prefix)."""
    qs = str(request.url.query)
    target = f"/api/v1/services{path}"
    if qs:
        target += f"?{qs}"
    return StarletteRedirect(url=target, status_code=307)


@app.api_route("/api/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def redirect_api_root(request: Request, path: str):
    """Redirect /api/... -> /api/v1/... for bare /api/ and /api hits.
    Skip paths that already contain v1/ to prevent redirect loops."""
    if path.startswith("v1/") or path == "v1":
        raise HTTPException(status_code=404, detail="Not found")
    qs = str(request.url.query)
    target = f"/api/v1/{path}"
    if qs:
        target += f"?{qs}"
    return StarletteRedirect(url=target, status_code=307)


@app.get("/v1{path:path}", include_in_schema=False)
async def redirect_v1(request: Request, path: str):
    """Redirect /v1... -> /api/v1... ."""
    qs = str(request.url.query)
    target = f"/api/v1{path}"
    if qs:
        target += f"?{qs}"
    return StarletteRedirect(url=target, status_code=307)
