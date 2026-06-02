import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta

from app.utils import utc_now

import sqlalchemy
from sqlalchemy import delete, select

from app.config import USAGE_FLUSH_INTERVAL, USAGE_RETENTION_DAYS
from app.database import async_session
from app.models import RouteUsage, UsageDetail, AgentUsage

logger = logging.getLogger("satring.usage")

# In-memory buffer: {(route, source, hour_iso): count}
_buffer: dict[tuple[str, str, str], int] = defaultdict(int)
# IP sets persist across flushes within the same hour for accurate unique counts
_ip_sets: dict[tuple[str, str, str], set[str]] = defaultdict(set)

# Detail buffer: {(dimension, value, hour_iso): count}
_detail_buffer: dict[tuple[str, str, str], int] = defaultdict(int)
_detail_ip_sets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
# Agent traffic buffer: {(agent_class, hour_iso): count}
_agent_buffer: dict[tuple[str, str], int] = defaultdict(int)
_agent_ip_sets: dict[tuple[str, str], set[str]] = defaultdict(set)

_lock = asyncio.Lock()
# Serializes flush() calls so two flushes can never read the same buffered
# batch (the periodic loop and the shutdown final-flush can otherwise race).
_flush_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None

EXCLUDED_PREFIXES = ("/static/", "/favicon", "/openapi.json", "/docs")

# User-agent classification patterns (order: most specific first)
_AGENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"GPTBot", re.I), "gptbot"),
    (re.compile(r"ClaudeBot", re.I), "claudebot"),
    (re.compile(r"Amazonbot", re.I), "amazonbot"),
    (re.compile(r"meta-externalagent|facebookexternalhit", re.I), "meta"),
    (re.compile(r"Applebot", re.I), "applebot"),
    (re.compile(r"GoogleOther|Googlebot|AdsBot-Google", re.I), "google"),
    (re.compile(r"402-indexer|402index|Open402DirectoryCrawler", re.I), "402-indexer"),
    (re.compile(r"ClawNet", re.I), "clawnet"),
    (re.compile(r"ShapBot", re.I), "shapbot"),
    (re.compile(r"lnget", re.I), "lnget"),
    (re.compile(r"^node$", re.I), "node"),
    (re.compile(r"python-httpx|python-requests|Python-urllib|aiohttp", re.I), "python"),
    (re.compile(r"^axios/", re.I), "axios"),
    (re.compile(r"Go-http-client", re.I), "go-http"),
    (re.compile(r"^curl/", re.I), "curl"),
    (re.compile(r"Satring-Scraper", re.I), "satring-scraper"),
    (re.compile(r"PetalBot|SERanking|DotBot|DataForSeo|Barkrowler|MJ12bot|SemrushBot|AhrefsBot", re.I), "seo-bot"),
    (re.compile(r"Mozilla.*Chrome|Mozilla.*Safari|Mozilla.*Firefox|Mozilla.*Edg", re.I), "browser"),
]


def classify_agent(user_agent: str) -> str:
    """Classify a User-Agent string into an agent class."""
    if not user_agent or user_agent == "-":
        return "unknown"
    for pattern, agent_class in _AGENT_PATTERNS:
        if pattern.search(user_agent):
            return agent_class
    return "other"

# Max distinct keys allowed in the buffer between flushes (safety cap)
MAX_BUFFER_KEYS = 10_000
# Max unique IPs tracked per (route, source, hour) bucket.
# Once reached, hit_count still increments but new IPs are not stored.
MAX_IPS_PER_BUCKET = 50_000

# Patterns to normalize dynamic path segments into placeholders.
# Order matters: longer prefixes first to avoid partial matches.
_NORMALIZE_PATTERNS = [
    # API routes with {slug} sub-resources
    (re.compile(r"^/api/v1/services/[^/]+/(recover/generate|recover/verify|ratings|reputation)"), r"/api/v1/services/{slug}/\1"),
    # API service detail
    (re.compile(r"^/api/v1/services/[^/]+$"), "/api/v1/services/{slug}"),
    # Web routes with {slug} sub-resources
    (re.compile(r"^/services/[^/]+/(edit|delete|recover|rate|reputation-invoice|reputation-result)"), r"/services/{slug}/\1"),
    # Web service detail
    (re.compile(r"^/services/[^/]+$"), "/services/{slug}"),
    # Payment status
    (re.compile(r"^/payment-status/[^/]+$"), "/payment-status/{hash}"),
]


def _normalize_path(path: str) -> str:
    """Replace dynamic path segments with placeholders to bound cardinality."""
    for pattern, replacement in _NORMALIZE_PATTERNS:
        result = pattern.sub(replacement, path)
        if result != path:
            return result
    return path


def record_hit(route: str, source: str, client_ip: str) -> None:
    """Record a single hit. Called from middleware (non-async, sync-safe)."""
    for prefix in EXCLUDED_PREFIXES:
        if route.startswith(prefix):
            return

    route = _normalize_path(route)

    # Safety: cap buffer size to prevent memory exhaustion from novel paths
    hour = utc_now().replace(minute=0, second=0, microsecond=0)
    hour_key = hour.isoformat()
    key = (route, source, hour_key)
    if key not in _buffer and len(_buffer) >= MAX_BUFFER_KEYS:
        return
    _buffer[key] += 1
    if len(_ip_sets[key]) < MAX_IPS_PER_BUCKET:
        _ip_sets[key].add(client_ip)


# Patterns to extract slug from raw (pre-normalized) paths
_SLUG_PATTERNS = [
    re.compile(r"^/api/v1/services/([^/]+)"),
    re.compile(r"^/services/([^/]+)"),
]


def record_details(path: str, query_params: dict[str, str], client_ip: str) -> None:
    """Record search queries, category filters, and viewed service slugs."""
    hour = utc_now().replace(minute=0, second=0, microsecond=0)
    hour_key = hour.isoformat()

    details: list[tuple[str, str]] = []

    # Search queries
    q = query_params.get("q", "").strip().lower()
    if q:
        details.append(("query", q[:200]))

    # Category filters
    cat = query_params.get("category", "").strip().lower()
    if cat:
        details.append(("category", cat[:100]))

    # Service slug views
    for pattern in _SLUG_PATTERNS:
        m = pattern.match(path)
        if m:
            slug = m.group(1)
            # Skip non-slug fixed segments
            if slug not in ("bulk",):
                details.append(("slug", slug[:200]))
            break

    for dimension, value in details:
        key = (dimension, value, hour_key)
        if key not in _detail_buffer and len(_detail_buffer) >= MAX_BUFFER_KEYS:
            continue
        _detail_buffer[key] += 1
        if len(_detail_ip_sets[key]) < MAX_IPS_PER_BUCKET:
            _detail_ip_sets[key].add(client_ip)


def record_agent(user_agent: str, client_ip: str) -> None:
    """Record a hit classified by user-agent. Called from middleware."""
    agent_class = classify_agent(user_agent)
    hour = utc_now().replace(minute=0, second=0, microsecond=0)
    hour_key = hour.isoformat()
    key = (agent_class, hour_key)
    if key not in _agent_buffer and len(_agent_buffer) >= MAX_BUFFER_KEYS:
        return
    _agent_buffer[key] += 1
    if len(_agent_ip_sets[key]) < MAX_IPS_PER_BUCKET:
        _agent_ip_sets[key].add(client_ip)


async def flush() -> None:
    """Snapshot buffer, upsert rows into RouteUsage, and purge old data.

    The buffers are drained atomically when snapshotted, so every batch is
    counted exactly once: a *failed* write merges the snapshot back for retry,
    a *successful* commit drops it. A batch that already committed is never
    re-queued (the ``committed`` guard) — previously a post-commit error would
    re-merge a committed batch and double the counts on every cycle, inflating
    hit_count toward powers of two. ``_flush_lock`` keeps flushes from
    overlapping and reading the same batch twice.
    """
    async with _flush_lock:
        async with _lock:
            has_data = bool(_buffer) or bool(_detail_buffer) or bool(_agent_buffer)
            if not has_data:
                return
            # Drain now: snapshot, then clear under the same lock so concurrent
            # request increments accumulate fresh and this batch can't be re-read.
            snapshot = dict(_buffer)
            detail_snapshot = dict(_detail_buffer)
            agent_snapshot = dict(_agent_buffer)
            _buffer.clear()
            _detail_buffer.clear()
            _agent_buffer.clear()
            # Snapshot IP set sizes but keep sets alive for the current hour
            current_hour = utc_now().replace(
                minute=0, second=0, microsecond=0
            ).isoformat()
            ip_counts = {k: len(v) for k, v in _ip_sets.items()}
            detail_ip_counts = {k: len(v) for k, v in _detail_ip_sets.items()}
            agent_ip_counts = {k: len(v) for k, v in _agent_ip_sets.items()}

        committed = False
        try:
            async with async_session() as db:
                for (route, source, hour_key), count in snapshot.items():
                    hour = datetime.fromisoformat(hour_key)
                    unique = ip_counts.get((route, source, hour_key), 0)
                    result = await db.execute(
                        select(RouteUsage).where(
                            RouteUsage.route == route,
                            RouteUsage.source == source,
                            RouteUsage.hour == hour,
                        )
                    )
                    row = result.scalars().first()
                    if row:
                        row.hit_count += count
                        # For the current hour the IP set is still accumulating,
                        # so overwrite with the latest total. For past hours the
                        # final count was captured before eviction.
                        row.unique_ips = unique
                    else:
                        db.add(RouteUsage(
                            route=route, source=source,
                            hour=hour, hit_count=count, unique_ips=unique,
                        ))

                # Flush detail buffer
                for (dimension, value, hour_key), count in detail_snapshot.items():
                    hour = datetime.fromisoformat(hour_key)
                    unique = detail_ip_counts.get((dimension, value, hour_key), 0)
                    result = await db.execute(
                        select(UsageDetail).where(
                            UsageDetail.dimension == dimension,
                            UsageDetail.value == value,
                            UsageDetail.hour == hour,
                        )
                    )
                    row = result.scalars().first()
                    if row:
                        row.hit_count += count
                        row.unique_ips = unique
                    else:
                        db.add(UsageDetail(
                            dimension=dimension, value=value,
                            hour=hour, hit_count=count, unique_ips=unique,
                        ))

                # Flush agent buffer
                for (agent_class, hour_key), count in agent_snapshot.items():
                    hour = datetime.fromisoformat(hour_key)
                    unique = agent_ip_counts.get((agent_class, hour_key), 0)
                    result = await db.execute(
                        select(AgentUsage).where(
                            AgentUsage.agent_class == agent_class,
                            AgentUsage.hour == hour,
                        )
                    )
                    row = result.scalars().first()
                    if row:
                        row.hit_count += count
                        row.unique_ips = unique
                    else:
                        db.add(AgentUsage(
                            agent_class=agent_class,
                            hour=hour, hit_count=count, unique_ips=unique,
                        ))

                # Purge old data (bulk DELETE, no loading into Python)
                cutoff = utc_now() - timedelta(days=USAGE_RETENTION_DAYS)
                await db.execute(
                    delete(RouteUsage).where(RouteUsage.hour < cutoff)
                )
                await db.execute(
                    delete(UsageDetail).where(UsageDetail.hour < cutoff)
                )
                await db.execute(
                    delete(AgentUsage).where(AgentUsage.hour < cutoff)
                )

                await db.commit()
                committed = True
        except Exception:
            # Only re-merge if the batch was NOT committed. Re-queuing a
            # committed batch is what caused the doubling bug.
            if not committed:
                async with _lock:
                    for key, count in snapshot.items():
                        _buffer[key] += count
                    for key, count in detail_snapshot.items():
                        _detail_buffer[key] += count
                    for key, count in agent_snapshot.items():
                        _agent_buffer[key] += count
            raise

        # Committed: the batch was already drained above; just evict IP sets
        # for past hours (no longer needed).
        async with _lock:
            for sets_dict in (_ip_sets, _detail_ip_sets):
                stale = [k for k in sets_dict if k[2] != current_hour]
                for k in stale:
                    del sets_dict[k]
            # Agent IP sets use 2-tuples (no source field)
            stale_agent = [k for k in _agent_ip_sets if k[1] != current_hour]
            for k in stale_agent:
                del _agent_ip_sets[k]

    # Update denormalized hit counts on Service (best-effort, non-blocking)
    try:
        await _update_service_hit_counts()
    except Exception:
        logger.exception("Service hit count update failed")


async def _update_service_hit_counts() -> None:
    """Bulk-update denormalized hit counts on Service from UsageDetail aggregation.

    Uses a single SQL UPDATE with correlated subqueries for efficiency.
    Runs after each flush cycle to keep counts fresh.
    """
    now = utc_now()
    seven_ago = now - timedelta(days=7)
    thirty_ago = now - timedelta(days=30)

    async with async_session() as db:
        await db.execute(sqlalchemy.text("""
            UPDATE services SET
                hit_count_total = COALESCE((
                    SELECT SUM(hit_count) FROM usage_detail
                    WHERE dimension = 'slug' AND value = services.slug
                ), 0),
                hit_count_7d = COALESCE((
                    SELECT SUM(hit_count) FROM usage_detail
                    WHERE dimension = 'slug' AND value = services.slug
                    AND hour >= :seven_ago
                ), 0),
                hit_count_30d = COALESCE((
                    SELECT SUM(hit_count) FROM usage_detail
                    WHERE dimension = 'slug' AND value = services.slug
                    AND hour >= :thirty_ago
                ), 0)
            WHERE status != 'purged'
        """), {"seven_ago": seven_ago, "thirty_ago": thirty_ago})
        await db.commit()


async def _flush_loop() -> None:
    """Periodically flush the buffer to the database."""
    try:
        while True:
            await asyncio.sleep(USAGE_FLUSH_INTERVAL)
            try:
                await flush()
            except Exception:
                logger.exception("Usage flush failed")
    except asyncio.CancelledError:
        pass


def start_flush_task() -> None:
    global _flush_task
    _flush_task = asyncio.create_task(_flush_loop())


async def stop_flush_task() -> None:
    global _flush_task
    if _flush_task:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None
    # Final flush on shutdown
    await flush()
