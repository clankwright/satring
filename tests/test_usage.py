import os

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.config import settings
from app.database import Base, get_db
from app.main import app, limiter, SEED_CATEGORIES
from app.models import RouteUsage, Category, UsageDetail
from app.usage import record_hit, record_details, flush, _buffer, _ip_sets, _detail_buffer, _detail_ip_sets, _normalize_path  # noqa: E501

settings.AUTH_ROOT_KEY = "test-mode"

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://satring:cYBv_gUZY0QgsnzD2a0Fr2jm_1_aU00jlohHQVGE_wo@localhost/satring_test",
)


async def _make_db():
    engine = create_async_engine(_TEST_DB_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session = session_factory()
    for name, slug, description in SEED_CATEGORIES:
        session.add(Category(name=name, slug=slug, description=description))
    await session.commit()
    return engine, session


async def _teardown_db(engine, session):
    await session.close()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def usage_db():
    engine, session = await _make_db()
    yield engine, session
    await _teardown_db(engine, session)


@pytest_asyncio.fixture
async def usage_client(usage_db):
    _engine, session = usage_db

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    limiter.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def clear_buffer():
    """Ensure all buffers and IP sets are clean before each test."""
    _buffer.clear()
    _ip_sets.clear()
    _detail_buffer.clear()
    _detail_ip_sets.clear()
    yield
    _buffer.clear()
    _ip_sets.clear()
    _detail_buffer.clear()
    _detail_ip_sets.clear()


@pytest.mark.asyncio
async def test_record_hit_and_flush(usage_db, monkeypatch):
    """record_hit accumulates counts, flush writes them to the DB."""
    from app import usage as usage_mod

    engine, session = usage_db
    test_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(usage_mod, "async_session", test_session_factory)

    record_hit("/api/v1/services", "api", "1.2.3.4")
    record_hit("/api/v1/services", "api", "1.2.3.4")
    record_hit("/", "web", "5.6.7.8")

    await flush()

    async with test_session_factory() as check_db:
        rows = (await check_db.execute(select(RouteUsage))).scalars().all()
        assert len(rows) == 2

        api_row = next(r for r in rows if r.source == "api")
        assert api_row.route == "/api/v1/services"
        assert api_row.hit_count == 2
        assert api_row.unique_ips == 1

        web_row = next(r for r in rows if r.source == "web")
        assert web_row.route == "/"
        assert web_row.hit_count == 1
        assert web_row.unique_ips == 1


@pytest.mark.asyncio
async def test_exclude_static():
    """Static and excluded paths should not be recorded (except .well-known, which is now tracked)."""
    record_hit("/static/css/theme.css", "web", "1.1.1.1")
    record_hit("/favicon.ico", "web", "1.1.1.1")
    record_hit("/openapi.json", "web", "1.1.1.1")
    record_hit("/docs", "web", "1.1.1.1")

    assert len(_buffer) == 0

    # .well-known paths are now tracked (agent discovery signals)
    record_hit("/.well-known/satring-verify", "web", "1.1.1.1")
    assert len(_buffer) == 1


@pytest.mark.asyncio
async def test_aggregation(usage_db, monkeypatch):
    """Multiple flushes for the same hour bucket merge correctly."""
    from app import usage as usage_mod

    engine, session = usage_db
    test_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(usage_mod, "async_session", test_session_factory)

    record_hit("/api/v1/search", "api", "10.0.0.1")
    record_hit("/api/v1/search", "api", "10.0.0.2")
    await flush()

    record_hit("/api/v1/search", "api", "10.0.0.1")
    await flush()

    async with test_session_factory() as check_db:
        rows = (await check_db.execute(
            select(RouteUsage).where(RouteUsage.route == "/api/v1/search")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].hit_count == 3
        # IP sets persist across flushes within the same hour, so 10.0.0.1
        # is deduplicated even though it appeared in both flush intervals
        assert rows[0].unique_ips == 2


@pytest.mark.asyncio
async def test_flush_merges_naive_hour_rows(usage_db, monkeypatch):
    """Existing usage rows store hour as timestamp without time zone.

    A flush must update those rows instead of raising TypeError when the
    ORM compares them with a timezone-aware cutoff.
    """
    from app import usage as usage_mod

    engine, _session = usage_db
    test_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(usage_mod, "async_session", test_session_factory)

    naive_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    async with test_session_factory() as db:
        db.add(RouteUsage(
            route="/naive-hour", source="web", hour=naive_hour,
            hit_count=4, unique_ips=1,
        ))
        await db.commit()

    record_hit("/naive-hour", "web", "1.2.3.4")
    await flush()

    async with test_session_factory() as check_db:
        rows = (await check_db.execute(
            select(RouteUsage).where(RouteUsage.route == "/naive-hour")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].hit_count == 5
        assert rows[0].hour.tzinfo is None


@pytest.mark.asyncio
async def test_unique_ips_dedup(usage_db, monkeypatch):
    """Same IP hitting the same endpoint multiple times counts as 1 unique."""
    from app import usage as usage_mod

    engine, session = usage_db
    test_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(usage_mod, "async_session", test_session_factory)

    for _ in range(10):
        record_hit("/", "web", "9.9.9.9")
    record_hit("/", "web", "8.8.8.8")

    await flush()

    async with test_session_factory() as check_db:
        row = (await check_db.execute(
            select(RouteUsage).where(RouteUsage.route == "/")
        )).scalars().first()
        assert row.hit_count == 11
        assert row.unique_ips == 2


@pytest.mark.asyncio
async def test_analytics_includes_usage(usage_client: AsyncClient):
    """The analytics response should include the usage field with unique IP stats."""
    resp = await usage_client.get("/api/v1/analytics")
    assert resp.status_code == 200
    data = resp.json()
    assert "usage" in data
    assert "api" in data["usage"]
    assert "web" in data["usage"]
    for src in ("api", "web"):
        assert "total_24h" in data["usage"][src]
        assert "unique_ips_24h" in data["usage"][src]
        assert "unique_ips_7d" in data["usage"][src]
        assert "unique_ips_30d" in data["usage"][src]
        assert "top_routes_30d" in data["usage"][src]
        assert "hourly_24h" in data["usage"][src]
        assert "daily_30d" in data["usage"][src]


@pytest.mark.asyncio
async def test_path_normalization():
    """Dynamic path segments should be replaced with placeholders."""
    # API routes
    assert _normalize_path("/api/v1/services/my-slug") == "/api/v1/services/{slug}"
    assert _normalize_path("/api/v1/services/my-slug/ratings") == "/api/v1/services/{slug}/ratings"
    assert _normalize_path("/api/v1/services/my-slug/reputation") == "/api/v1/services/{slug}/reputation"
    assert _normalize_path("/api/v1/services/my-slug/recover/generate") == "/api/v1/services/{slug}/recover/generate"
    assert _normalize_path("/api/v1/services/my-slug/recover/verify") == "/api/v1/services/{slug}/recover/verify"
    # Web routes
    assert _normalize_path("/services/my-slug") == "/services/{slug}"
    assert _normalize_path("/services/my-slug/edit") == "/services/{slug}/edit"
    assert _normalize_path("/services/my-slug/rate") == "/services/{slug}/rate"
    assert _normalize_path("/services/my-slug/recover") == "/services/{slug}/recover"
    assert _normalize_path("/services/my-slug/reputation-invoice") == "/services/{slug}/reputation-invoice"
    # Payment status
    assert _normalize_path("/payment-status/abc123def") == "/payment-status/{hash}"
    # Static paths stay unchanged
    assert _normalize_path("/") == "/"
    assert _normalize_path("/api/v1/services") == "/api/v1/services"
    assert _normalize_path("/api/v1/search") == "/api/v1/search"
    assert _normalize_path("/submit") == "/submit"


@pytest.mark.asyncio
async def test_normalized_hits_aggregate():
    """Hits to different slugs should aggregate under the same normalized key."""
    record_hit("/services/alpha", "web", "1.1.1.1")
    record_hit("/services/beta", "web", "2.2.2.2")
    record_hit("/services/gamma", "web", "1.1.1.1")

    # All three should map to the same buffer key
    web_keys = [k for k in _buffer if k[0] == "/services/{slug}" and k[1] == "web"]
    assert len(web_keys) == 1
    assert _buffer[web_keys[0]] == 3

    # IPs should be deduplicated within the key
    ip_set = _ip_sets[web_keys[0]]
    assert len(ip_set) == 2


@pytest.mark.asyncio
async def test_buffer_cap():
    """Buffer should not grow beyond MAX_BUFFER_KEYS."""
    from app import usage as usage_mod
    original_max = usage_mod.MAX_BUFFER_KEYS
    usage_mod.MAX_BUFFER_KEYS = 5
    try:
        for i in range(20):
            record_hit(f"/test-path-{i}", "web", "1.1.1.1")
        assert len(_buffer) == 5
    finally:
        usage_mod.MAX_BUFFER_KEYS = original_max


@pytest.mark.asyncio
async def test_404_not_tracked(usage_client: AsyncClient):
    """Requests returning 404 should not be recorded."""
    await usage_client.get("/services/nonexistent-slug-xyz")
    assert len(_buffer) == 0


@pytest.mark.asyncio
async def test_successful_request_tracked(usage_client: AsyncClient):
    """Successful requests should be recorded."""
    await usage_client.get("/")
    assert len(_buffer) > 0


@pytest.mark.asyncio
async def test_record_details_search_query():
    """Search queries are captured in the detail buffer."""
    record_details("/search", {"q": "Lightning API"}, "1.1.1.1")
    record_details("/api/v1/search", {"q": "Lightning API"}, "2.2.2.2")

    query_keys = [k for k in _detail_buffer if k[0] == "query"]
    assert len(query_keys) == 1
    assert query_keys[0][1] == "lightning api"
    assert _detail_buffer[query_keys[0]] == 2
    assert len(_detail_ip_sets[query_keys[0]]) == 2


@pytest.mark.asyncio
async def test_record_details_category():
    """Category filters are captured in the detail buffer."""
    record_details("/", {"category": "tools"}, "1.1.1.1")
    record_details("/api/v1/services", {"category": "tools"}, "1.1.1.1")

    cat_keys = [k for k in _detail_buffer if k[0] == "category"]
    assert len(cat_keys) == 1
    assert cat_keys[0][1] == "tools"
    assert _detail_buffer[cat_keys[0]] == 2


@pytest.mark.asyncio
async def test_record_details_slug():
    """Service slug views are captured in the detail buffer."""
    record_details("/services/satsapi", {}, "1.1.1.1")
    record_details("/api/v1/services/satsapi", {}, "2.2.2.2")
    record_details("/services/other-service", {}, "1.1.1.1")

    slug_keys = [k for k in _detail_buffer if k[0] == "slug"]
    assert len(slug_keys) == 2
    satsapi_key = next(k for k in slug_keys if k[1] == "satsapi")
    assert _detail_buffer[satsapi_key] == 2


@pytest.mark.asyncio
async def test_record_details_skips_bulk():
    """The /services/bulk path should not record 'bulk' as a slug."""
    record_details("/api/v1/services/bulk", {}, "1.1.1.1")
    slug_keys = [k for k in _detail_buffer if k[0] == "slug"]
    assert len(slug_keys) == 0


@pytest.mark.asyncio
async def test_record_details_empty_params():
    """No details recorded when query params are absent."""
    record_details("/", {}, "1.1.1.1")
    assert len(_detail_buffer) == 0


@pytest.mark.asyncio
async def test_detail_flush(usage_db, monkeypatch):
    """Detail buffer flushes to UsageDetail table."""
    from app import usage as usage_mod

    engine, session = usage_db
    test_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(usage_mod, "async_session", test_session_factory)

    # Need at least one route hit so flush doesn't short-circuit
    record_hit("/", "web", "1.1.1.1")
    record_details("/search", {"q": "bitcoin"}, "1.1.1.1")
    record_details("/search", {"q": "bitcoin"}, "2.2.2.2")
    record_details("/", {"category": "data"}, "1.1.1.1")

    await flush()

    async with test_session_factory() as check_db:
        rows = (await check_db.execute(
            select(UsageDetail)
        )).scalars().all()
        assert len(rows) == 2

        query_row = next(r for r in rows if r.dimension == "query")
        assert query_row.value == "bitcoin"
        assert query_row.hit_count == 2
        assert query_row.unique_ips == 2

        cat_row = next(r for r in rows if r.dimension == "category")
        assert cat_row.value == "data"
        assert cat_row.hit_count == 1
