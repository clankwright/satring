import logging
import math
from datetime import timedelta, timezone
from urllib.parse import urlparse

logger = logging.getLogger("satring.web")

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import (
    settings, payments_enabled, MAX_NAME, MAX_URL, MAX_DESCRIPTION, MAX_OWNER_NAME,
    MAX_OWNER_CONTACT, MAX_LOGO_URL, MAX_REVIEWER_NAME, MAX_COMMENT,
    MAX_X402_NETWORK, MAX_X402_ASSET, MAX_X402_PAY_TO, MAX_PRICING_USD,
    MAX_MPP_METHOD, MAX_MPP_REALM, MAX_MPP_CURRENCY,
    RATE_EDIT, RATE_DELETE, RATE_RECOVER,
    RATE_SEARCH, RATE_PAYMENT_STATUS, RATE_SITEMAP, RATE_DETAIL_API,
    RATE_SUBMIT, RATE_REVIEW, RATE_INVOICE,
)
from app.database import get_db
from app.l402 import create_invoice, check_payment_status, check_and_consume_payment
from app.main import templates, limiter
from app.models import Service, Category, Rating, service_categories
from app.routes.api import build_reputation_data, build_analytics_data, build_service_analytics
from app.utils import unique_slug, generate_edit_token, hash_token, verify_edit_token, get_same_domain_services, domain_root, extract_domain, is_public_hostname, extract_email, send_verify_email, find_purged_service, find_existing_service, normalize_url, overwrite_purged_service, escape_like, normalize_protocol, protocol_filter, is_valid_protocol, BASE_PROTOCOLS, utc_now

router = APIRouter(include_in_schema=False)

PAGE_SIZE = 20


@router.get("/directory", response_class=HTMLResponse)
async def directory(
    request: Request,
    q: str = "",
    category: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    verified: str | None = None,
    protocol: str | None = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    protocol = normalize_protocol(protocol)

    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()

    query = select(Service).options(selectinload(Service.categories)).where(Service.status != "purged")
    if q.strip():
        # SECURITY: escape LIKE wildcards so user input is matched literally
        pattern = f"%{escape_like(q.strip())}%"
        query = query.where(Service.name.ilike(pattern, escape="\\") | Service.description.ilike(pattern, escape="\\"))
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    if status:
        query = query.where(Service.status == status)
    if verified == "true":
        query = query.where(Service.domain_verified == True)
    if protocol:
        query = query.where(protocol_filter(Service.protocol, protocol))

    sort_map = {
        "top-rated": Service.avg_rating.desc(),
        "cheapest": Service.pricing_sats.asc(),
        "most-reviewed": Service.rating_count.desc(),
        "popular": Service.hit_count_30d.desc(),
        "newest": Service.created_at.desc(),
    }
    # id.desc() tiebreak keeps ordering deterministic when primary keys collide
    # (e.g. seed imports share a created_at, ratings share a count).
    query = query.order_by(sort_map.get(sort, Service.hit_count_30d.desc()), Service.id.desc())

    # Count total for pagination
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages)

    services = (await db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()

    # Build qs_base for pagination links (preserving existing filters)
    qs_parts = []
    if q.strip():
        qs_parts.append(f"q={q.strip()}")
    if category:
        qs_parts.append(f"category={category}")
    if verified == "true":
        qs_parts.append("verified=true")
    elif status:
        qs_parts.append(f"status={status}")
    if protocol:
        qs_parts.append(f"protocol={protocol.replace('+', '%2B')}")
    if sort:
        qs_parts.append(f"sort={sort}")
    qs_base = "&".join(qs_parts)

    return templates.TemplateResponse(request, "services/list.html", {
        "services": services,
        "categories": categories,
        "active_category": category,
        "active_status": status,
        "active_sort": sort,
        "active_verified": verified,
        "active_protocol": protocol,
        "active_q": q.strip(),
        "page": page,
        "total_pages": total_pages,
        "qs_base": qs_base,
        "analytics_price_sats": settings.AUTH_ANALYTICS_PRICE_SATS,
    })


@router.get("/search", response_class=HTMLResponse)
@limiter.limit(RATE_SEARCH)
async def search(
    request: Request,
    q: str = "",
    category: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    verified: str | None = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    query = select(Service).options(selectinload(Service.categories)).where(Service.status != "purged")
    if q.strip():
        # SECURITY: escape LIKE wildcards so user input is matched literally
        pattern = f"%{escape_like(q.strip())}%"
        query = query.where(Service.name.ilike(pattern, escape="\\") | Service.description.ilike(pattern, escape="\\"))
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    if status:
        query = query.where(Service.status == status)
    if verified == "true":
        query = query.where(Service.domain_verified == True)

    sort_map = {
        "top-rated": Service.avg_rating.desc(),
        "cheapest": Service.pricing_sats.asc(),
        "most-reviewed": Service.rating_count.desc(),
        "popular": Service.hit_count_30d.desc(),
        "newest": Service.created_at.desc(),
    }
    # id.desc() tiebreak keeps ordering deterministic when primary keys collide
    # (e.g. seed imports share a created_at, ratings share a count).
    query = query.order_by(sort_map.get(sort, Service.hit_count_30d.desc()), Service.id.desc())

    # Count total for pagination
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages)

    services = (await db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()

    # Build qs_base for pagination links
    qs_parts = []
    if q.strip():
        qs_parts.append(f"q={q.strip()}")
    if category:
        qs_parts.append(f"category={category}")
    if verified == "true":
        qs_parts.append("verified=true")
    elif status:
        qs_parts.append(f"status={status}")
    if sort:
        qs_parts.append(f"sort={sort}")
    qs_base = "&".join(qs_parts)

    return templates.TemplateResponse(request, "services/_card_grid.html", {
        "services": services,
        "page": page,
        "total_pages": total_pages,
        "qs_base": qs_base,
    })


@router.get("/owner/{domain}", response_class=HTMLResponse)
@limiter.limit("6/minute")
async def owner_dashboard(
    request: Request,
    domain: str,
    token: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Owner traffic dashboard. Requires edit token via ?token= query param."""
    from app.utils import get_same_domain_services, verify_edit_token, extract_domain
    from app.models import UsageDetail

    services = await get_same_domain_services(db, f"https://{domain}")
    if not services:
        raise HTTPException(status_code=404, detail="No services found for this domain")

    # Verify ownership via token query param
    if payments_enabled():
        if not token:
            raise HTTPException(status_code=403, detail="Token required: /owner/{domain}?token=YOUR_EDIT_TOKEN")
        verified = any(
            s.edit_token_hash and verify_edit_token(token, s.edit_token_hash)
            for s in services
        )
        if not verified:
            raise HTTPException(status_code=403, detail="Invalid token for this domain")

    from datetime import timedelta
    now = utc_now()
    seven_ago = now - timedelta(days=7)
    thirty_ago = now - timedelta(days=30)
    slugs = [s.slug for s in services]

    total_hits = (await db.execute(
        select(func.coalesce(func.sum(UsageDetail.hit_count), 0))
        .where(UsageDetail.dimension == "slug", UsageDetail.value.in_(slugs))
    )).scalar()
    hits_7d = (await db.execute(
        select(func.coalesce(func.sum(UsageDetail.hit_count), 0))
        .where(UsageDetail.dimension == "slug", UsageDetail.value.in_(slugs),
               UsageDetail.hour >= seven_ago)
    )).scalar()
    hits_30d = (await db.execute(
        select(func.coalesce(func.sum(UsageDetail.hit_count), 0))
        .where(UsageDetail.dimension == "slug", UsageDetail.value.in_(slugs),
               UsageDetail.hour >= thirty_ago)
    )).scalar()
    unique_ips_30d = (await db.execute(
        select(func.coalesce(func.sum(UsageDetail.unique_ips), 0))
        .where(UsageDetail.dimension == "slug", UsageDetail.value.in_(slugs),
               UsageDetail.hour >= thirty_ago)
    )).scalar()
    daily_rows = (await db.execute(
        select(
            func.date(UsageDetail.hour).label("day"),
            func.sum(UsageDetail.hit_count).label("hits"),
        )
        .where(UsageDetail.dimension == "slug", UsageDetail.value.in_(slugs),
               UsageDetail.hour >= thirty_ago)
        .group_by(func.date(UsageDetail.hour))
        .order_by(func.date(UsageDetail.hour))
    )).all()

    return templates.TemplateResponse(request, "services/owner_dashboard.html", {
        "domain": domain,
        "service_count": len(services),
        "total_hits": int(total_hits),
        "hits_7d": int(hits_7d),
        "hits_30d": int(hits_30d),
        "unique_ips_30d": int(unique_ips_30d),
        "services": [{"slug": s.slug, "name": s.name, "hit_count_30d": s.hit_count_30d or 0,
                       "hit_count_7d": s.hit_count_7d or 0, "status": s.status} for s in services],
        "daily_hits": [{"date": str(r[0]), "hits": int(r[1])} for r in daily_rows],
        "audience_price_sats": settings.AUTH_OWNER_AUDIENCE_PRICE_SATS,
    })


@router.get("/services/{slug}", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def service_detail(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Service)
        .options(selectinload(Service.categories), selectinload(Service.ratings))
        .where(Service.slug == slug)
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    return templates.TemplateResponse(request, "services/detail.html", {
        "service": service,
        "reputation_price_sats": settings.AUTH_REPUTATION_PRICE_SATS,
        "analytics_price_sats": settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS,
        "meta_token": _meta_token(service.slug),
    })


def _meta_token(slug: str) -> str:
    """Generate a short-lived HMAC token for meta.json access.

    Valid for the current 10-minute window. The token is embedded in the
    server-rendered detail page so only real page loads can fetch meta data.
    """
    import hmac, hashlib, time
    window = str(int(time.time()) // 600)  # 10-minute window
    secret = settings.AUTH_ROOT_KEY or "fallback"
    return hmac.new(f"meta:{secret}".encode(), f"{slug}:{window}".encode(), hashlib.sha256).hexdigest()[:16]


@router.get("/services/{slug}/meta.json")
@limiter.limit(RATE_DETAIL_API)
async def service_meta(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    """Return sensitive service fields via token-gated endpoint.

    SECURITY: Requires a server-generated HMAC token (embedded in the detail
    page template) to prevent direct scraping. Token rotates every 10 minutes.
    """
    token = request.query_params.get("t", "")
    expected = _meta_token(slug)
    import hmac as _hmac
    if not token or not _hmac.compare_digest(token, expected):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    result = await db.execute(
        select(Service)
        .where(Service.slug == slug)
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = {
        "url": service.url,
        "owner_contact": service.owner_contact or "",
    }
    parts = service.protocol.split("+")
    if "x402" in parts and service.x402_pay_to:
        data["x402_pay_to"] = service.x402_pay_to
        data["x402_network"] = service.x402_network or "eip155:8453"
        if service.x402_asset:
            data["x402_asset"] = service.x402_asset
    if "MPP" in parts and service.mpp_method:
        data["mpp_method"] = service.mpp_method
        if service.mpp_realm:
            data["mpp_realm"] = service.mpp_realm
        if service.mpp_currency:
            data["mpp_currency"] = service.mpp_currency
    return JSONResponse(data)


@router.get("/submit", response_class=HTMLResponse)
async def submit_form(request: Request, db: AsyncSession = Depends(get_db)):
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    return templates.TemplateResponse(request, "services/submit.html", {
        "categories": categories,
    })


@router.get("/submit/recover", response_class=HTMLResponse)
async def submit_recover_form(request: Request, db: AsyncSession = Depends(get_db)):
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    return templates.TemplateResponse(request, "services/submit.html", {
        "categories": categories,
        "recovery_mode": True,
    })


@router.post("/submit")
@limiter.limit(RATE_SUBMIT)
async def submit_service(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    url: str = Form(...),
    description: str = Form(""),
    protocol: str = Form("L402"),
    pricing_sats: int = Form(0),
    pricing_model: str = Form("per-request"),
    owner_name: str = Form(""),
    owner_contact: str = Form(""),
    logo_url: str = Form(""),
    existing_edit_token: str = Form(""),
    x402_network: str = Form(""),
    x402_asset: str = Form(""),
    x402_pay_to: str = Form(""),
    pricing_usd: str = Form(""),
    mpp_method: str = Form(""),
    mpp_realm: str = Form(""),
    mpp_currency: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    form_data = await request.form()
    category_ids = [int(v) for k, v in form_data.multi_items() if k == "categories"]

    # Recovery flow: payment_hash arrives in query (normal re-POST from payment page)
    # or in form body (user re-submitting via /submit/recover after UI timeout).
    payment_hash_form = form_data.get("payment_hash") or request.query_params.get("payment_hash") or ""
    recovery_mode = bool(form_data.get("payment_hash"))

    async def _render_error(msg: str, status_code: int = 422, **extra):
        cats = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/submit.html", {
            "categories": cats,
            "error": msg,
            "form": {
                "name": name, "url": url, "description": description,
                "protocol": protocol, "pricing_sats": pricing_sats,
                "pricing_model": pricing_model, "owner_name": owner_name,
                "owner_contact": owner_contact, "logo_url": logo_url,
                "existing_edit_token": existing_edit_token,
                "x402_network": x402_network, "x402_asset": x402_asset,
                "x402_pay_to": x402_pay_to, "pricing_usd": pricing_usd,
                "mpp_method": mpp_method, "mpp_realm": mpp_realm, "mpp_currency": mpp_currency,
                "payment_hash": payment_hash_form,
            },
            "selected_category_ids": category_ids,
            "recovery_mode": recovery_mode,
            **extra,
        }, status_code=status_code)

    # Validate category count (1–2 required)
    if len(category_ids) < 1 or len(category_ids) > 2:
        return await _render_error("Select 1–2 categories.")

    # SECURITY: Server-side length limits prevent DB bloat and memory exhaustion.
    # HTML maxlength is client-side only and trivially bypassed. Constants in config.py.
    LENGTH_LIMITS = {
        "name": MAX_NAME, "url": MAX_URL, "description": MAX_DESCRIPTION,
        "owner_name": MAX_OWNER_NAME, "owner_contact": MAX_OWNER_CONTACT,
        "logo_url": MAX_LOGO_URL, "x402_network": MAX_X402_NETWORK,
        "x402_asset": MAX_X402_ASSET, "x402_pay_to": MAX_X402_PAY_TO,
        "pricing_usd": MAX_PRICING_USD,
        "mpp_method": MAX_MPP_METHOD, "mpp_realm": MAX_MPP_REALM,
        "mpp_currency": MAX_MPP_CURRENCY,
    }
    for field_name, max_len in LENGTH_LIMITS.items():
        val = locals()[field_name]
        if len(val) > max_len:
            return await _render_error(f"{field_name} exceeds maximum length of {max_len} characters.")

    # SECURITY: Reject non-http(s) schemes to prevent stored XSS via javascript:/data: URIs
    parsed_url = urlparse(url)
    parsed_logo = urlparse(logo_url) if logo_url else None
    if parsed_url.scheme not in ("http", "https") or (
        parsed_logo and parsed_logo.scheme not in ("http", "https")
    ):
        return await _render_error("URL and logo URL must start with http:// or https://")

    proto_parts = protocol.split("+")
    # Clear sat pricing when no L402 component
    if "L402" not in proto_parts:
        pricing_sats = 0
        pricing_model = "per-request"

    # Validate x402 fields before payment gate so users don't pay for a rejected submission
    if "x402" in proto_parts:
        missing = []
        if not x402_pay_to:
            missing.append("wallet address")
        if not pricing_usd:
            missing.append("USD price")
        if missing:
            return await _render_error(f"{', '.join(missing)} required for x402 protocol.")

    # Validate MPP fields before payment gate
    if "MPP" in proto_parts:
        if not mpp_method:
            return await _render_error("Payment method required for MPP protocol.")

    url = normalize_url(url)
    client_ip = request.client.host if request.client else "unknown"
    logger.info(
        "SUBMIT name=%r url=%r protocol=%s categories=%s "
        "pricing_sats=%s pricing_model=%s owner=%r contact=%r ip=%s recovery=%s",
        name, url, protocol, category_ids,
        pricing_sats, pricing_model, owner_name, owner_contact, client_ip, recovery_mode,
    )

    # Reject duplicate URLs before payment gate so users don't pay for a rejected submission
    existing = await find_existing_service(db, url)
    if existing:
        return await _render_error(
            f"A service with this URL already exists: {existing.name}",
            status_code=409,
            existing_slug=existing.slug,
        )

    # Payment gate (skipped in test mode)
    if payments_enabled():
        payment_hash = payment_hash_form or None
        if not payment_hash:
            # Create invoice and show payment page
            invoice = await create_invoice(
                settings.AUTH_SUBMIT_PRICE_SATS, "satring.com service submission"
            )
            # Build hidden fields for resubmission
            form_fields = {
                "name": name, "url": url, "description": description,
                "protocol": protocol, "pricing_sats": str(pricing_sats),
                "pricing_model": pricing_model, "owner_name": owner_name,
                "owner_contact": owner_contact, "logo_url": logo_url,
                "existing_edit_token": existing_edit_token,
                "x402_network": x402_network, "x402_asset": x402_asset,
                "x402_pay_to": x402_pay_to, "pricing_usd": pricing_usd,
                "mpp_method": mpp_method, "mpp_realm": mpp_realm, "mpp_currency": mpp_currency,
            }
            for cid in category_ids:
                form_fields[f"categories_{cid}"] = str(cid)
            return templates.TemplateResponse(request, "services/payment_required.html", {
                "payment_hash": invoice["payment_hash"],
                "payment_request": invoice["payment_request"],
                "amount_sats": settings.AUTH_SUBMIT_PRICE_SATS,
                "form_action": "/submit",
                "form_fields": form_fields,
                "category_ids": category_ids,
            })

        # Verify payment. SECURITY: also verify amount matches the submission
        # price so a hash from a cheaper endpoint can't be replayed here.
        paid, paid_sats = await check_payment_status(payment_hash)
        if not paid or paid_sats < settings.AUTH_SUBMIT_PRICE_SATS:
            return HTMLResponse(
                f"<h1>Payment not verified</h1><p>Invoice not paid or insufficient amount (need {settings.AUTH_SUBMIT_PRICE_SATS} sats).</p>",
                status_code=402,
            )
        if not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse(
                "<h1>Payment already used</h1><p>This payment has already been consumed.</p>",
                status_code=402,
            )

    slug = await unique_slug(db, name)

    # Fetch same-domain services (used for token reuse + auto-verify)
    domain_services = await get_same_domain_services(db, url)

    # Check if existing token matches a same-domain service
    token_reused = False
    if existing_edit_token:
        for ds in domain_services:
            if ds.edit_token_hash and verify_edit_token(existing_edit_token, ds.edit_token_hash):
                token_reused = True
                break

    if token_reused:
        edit_token = existing_edit_token
        edit_token_hash = ds.edit_token_hash
    else:
        edit_token = generate_edit_token()
        edit_token_hash = hash_token(edit_token)

    # Auto-verify if any same-domain service is already verified
    auto_verified = False
    inherited_challenge = None
    for ds in domain_services:
        if ds.domain_verified:
            auto_verified = True
            inherited_challenge = ds.domain_challenge
            break

    # Check for purged service with the same URL — overwrite instead of creating new
    purged = await find_purged_service(db, url)
    if purged:
        await overwrite_purged_service(
            db, purged,
            name=name, slug=slug, description=description,
            pricing_sats=pricing_sats, pricing_model=pricing_model,
            protocol=protocol, owner_name=owner_name, owner_contact=owner_contact,
            logo_url=logo_url, edit_token_hash=edit_token_hash,
            category_ids=category_ids,
            domain_verified=auto_verified,
            domain_challenge=inherited_challenge,
            mpp_method=mpp_method or None, mpp_realm=mpp_realm or None,
            mpp_currency=mpp_currency or None,
        )
        service = purged
    else:
        service = Service(
            name=name, slug=slug, url=url, description=description,
            protocol=protocol, pricing_sats=pricing_sats, pricing_model=pricing_model,
            owner_name=owner_name, owner_contact=owner_contact, logo_url=logo_url,
            x402_network=x402_network or None, x402_asset=x402_asset or None,
            x402_pay_to=x402_pay_to or None, pricing_usd=pricing_usd or None,
            mpp_method=mpp_method or None, mpp_realm=mpp_realm or None,
            mpp_currency=mpp_currency or None,
            edit_token_hash=edit_token_hash,
            domain_verified=auto_verified,
            domain_challenge=inherited_challenge,
        )
        if category_ids:
            cats = (await db.execute(select(Category).where(Category.id.in_(category_ids)))).scalars().all()
            service.categories = list(cats)
        db.add(service)

    try:
        await db.commit()
    except Exception:
        logger.exception("SUBMIT FAILED commit for name=%r url=%r ip=%s", name, url, client_ip)
        raise
    logger.info("SUBMIT OK slug=%s id=%s url=%r ip=%s", service.slug, service.id, url, client_ip)

    # Send verification instructions if owner_contact contains an email
    email = extract_email(owner_contact or "")
    if email and not auto_verified:
        domain = extract_domain(url)
        background_tasks.add_task(send_verify_email, email, service.slug, domain)

    return templates.TemplateResponse(request, "services/submit_success.html", {
        "service": service,
        "edit_token": edit_token,
        "token_reused": token_reused,
    })


EDITABLE_FIELDS = {
    "name", "description", "pricing_sats", "pricing_model",
    "protocol", "owner_name", "owner_contact", "logo_url",
}


@router.get("/services/{slug}/edit", response_class=HTMLResponse)
async def edit_service_form(
    request: Request,
    slug: str,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).options(selectinload(Service.categories))
        .where(Service.slug == slug)
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    token_valid = (
        token is not None
        and service.edit_token_hash is not None
        and verify_edit_token(token, service.edit_token_hash)
    )
    token_invalid = token is not None and not token_valid
    return templates.TemplateResponse(request, "services/edit.html", {
        "service": service,
        "categories": categories,
        "token_valid": token_valid,
        "token_invalid": token_invalid,
        "token": token if token_valid else "",
    })


@router.post("/services/{slug}/edit")
@limiter.limit(RATE_EDIT)
async def edit_service(
    request: Request,
    slug: str,
    edit_token: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    protocol: str = Form(""),
    pricing_sats: int = Form(0),
    pricing_model: str = Form(""),
    owner_name: str = Form(""),
    owner_contact: str = Form(""),
    logo_url: str = Form(""),
    x402_network: str = Form(""),
    x402_asset: str = Form(""),
    x402_pay_to: str = Form(""),
    pricing_usd: str = Form(""),
    mpp_method: str = Form(""),
    mpp_realm: str = Form(""),
    mpp_currency: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).options(selectinload(Service.categories))
        .where(Service.slug == slug)
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    if not service.edit_token_hash or not verify_edit_token(edit_token, service.edit_token_hash):
        categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/edit.html", {
            "service": service,
            "categories": categories,
            "token_valid": False,
            "token_invalid": True,
            "token": "",
        }, status_code=403)

    form_data = await request.form()
    category_ids = [int(v) for k, v in form_data.multi_items() if k == "categories"]

    # Validate category count (1–2 required)
    if len(category_ids) < 1 or len(category_ids) > 2:
        # Apply submitted values for template rendering (not committed)
        if name:
            service.name = name
        service.description = description
        service.protocol = protocol or service.protocol
        service.pricing_sats = pricing_sats
        service.pricing_model = pricing_model or service.pricing_model
        service.owner_name = owner_name
        service.owner_contact = owner_contact
        service.logo_url = logo_url
        categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/edit.html", {
            "service": service,
            "categories": categories,
            "token_valid": True,
            "token_invalid": False,
            "token": edit_token,
            "error": "Select 1–2 categories.",
            "selected_category_ids": category_ids,
        }, status_code=422)

    if name:
        service.name = name
    if description is not None:
        service.description = description
    if protocol:
        service.protocol = protocol
    # Clear sat pricing when no L402 component
    edit_parts = service.protocol.split("+")
    if "L402" not in edit_parts:
        service.pricing_sats = 0
        service.pricing_model = "per-request"
    else:
        service.pricing_sats = pricing_sats
        if pricing_model:
            service.pricing_model = pricing_model
    service.owner_name = owner_name
    service.owner_contact = owner_contact
    service.logo_url = logo_url
    service.x402_network = x402_network or None
    service.x402_asset = x402_asset or None
    service.x402_pay_to = x402_pay_to or None
    service.pricing_usd = pricing_usd or None
    service.mpp_method = mpp_method or None
    service.mpp_realm = mpp_realm or None
    service.mpp_currency = mpp_currency or None

    # Validate x402 fields are present when protocol includes x402
    if "x402" in edit_parts:
        missing = []
        if not service.x402_pay_to:
            missing.append("wallet address")
        if not service.pricing_usd:
            missing.append("USD price")
        if missing:
            categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
            return templates.TemplateResponse(request, "services/edit.html", {
                "service": service,
                "categories": categories,
                "token_valid": True,
                "token_invalid": False,
                "token": edit_token,
                "error": f"{', '.join(missing)} required for x402 protocol.",
                "selected_category_ids": category_ids,
            }, status_code=422)

    # Validate MPP fields when protocol includes MPP
    if "MPP" in edit_parts:
        if not service.mpp_method:
            categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
            return templates.TemplateResponse(request, "services/edit.html", {
                "service": service,
                "categories": categories,
                "token_valid": True,
                "token_invalid": False,
                "token": edit_token,
                "error": "Payment method required for MPP protocol.",
                "selected_category_ids": category_ids,
            }, status_code=422)

    cats = (await db.execute(select(Category).where(Category.id.in_(category_ids)))).scalars().all()
    service.categories = list(cats)

    await db.commit()
    return RedirectResponse(f"/services/{slug}", status_code=303)


@router.post("/services/{slug}/delete")
@limiter.limit(RATE_DELETE)
async def delete_service(
    request: Request,
    slug: str,
    edit_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    if not service.edit_token_hash or not verify_edit_token(edit_token, service.edit_token_hash):
        return HTMLResponse("Forbidden", status_code=403)

    await db.delete(service)
    await db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/services/{slug}/recover", response_class=HTMLResponse)
async def recover_form(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    expires = service.domain_challenge_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    challenge_active = (
        service.domain_challenge is not None
        and expires is not None
        and expires > utc_now()
    )
    return templates.TemplateResponse(request, "services/recover.html", {
        "service": service,
        "challenge_active": challenge_active,
        "verify_path": f"{domain_root(service.url)}/.well-known/satring-verify",
    })


@router.post("/services/{slug}/recover")
@limiter.limit(RATE_RECOVER)
async def recover_service(
    request: Request,
    slug: str,
    action: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    verify_path = f"{domain_root(service.url)}/.well-known/satring-verify"

    if action == "generate":
        import secrets
        challenge = secrets.token_hex(32)
        service.domain_challenge = challenge
        service.domain_challenge_expires_at = utc_now() + timedelta(minutes=30)
        await db.commit()
        return templates.TemplateResponse(request, "services/recover.html", {
            "service": service,
            "challenge_active": True,
            "verify_path": verify_path,
        })

    elif action == "verify":
        v_expires = service.domain_challenge_expires_at
        if v_expires is not None and v_expires.tzinfo is None:
            v_expires = v_expires.replace(tzinfo=timezone.utc)
        if (
            not service.domain_challenge
            or not v_expires
            or v_expires <= utc_now()
        ):
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": False,
                "error": "Challenge expired. Please generate a new one.",
                "verify_path": verify_path,
            })

        # SECURITY: Block SSRF — prevent server from fetching internal/private IPs
        hostname = extract_domain(service.url)
        if not hostname or not is_public_hostname(hostname):
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": "Cannot verify domain: hostname resolves to a private or unreachable address.",
                "verify_path": verify_path,
            })

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(verify_path)
            fetched = resp.text.strip()
        except Exception:
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": f"Could not reach {verify_path}",
                "verify_path": verify_path,
            })

        if fetched != service.domain_challenge:
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": "Challenge code does not match.",
                "verify_path": verify_path,
            })

        # Success - generate new edit token and apply to all same-domain services
        new_token = generate_edit_token()
        new_hash = hash_token(new_token)
        domain_services = await get_same_domain_services(db, service.url)
        for ds in domain_services:
            ds.edit_token_hash = new_hash
            ds.domain_verified = True
            ds.domain_challenge = service.domain_challenge
        service.edit_token_hash = new_hash
        service.domain_verified = True
        await db.commit()
        return templates.TemplateResponse(request, "services/recover.html", {
            "service": service,
            "challenge_active": False,
            "new_token": new_token,
            "domain_services": domain_services,
        })

    return HTMLResponse("Bad request", status_code=400)


@router.post("/services/{slug}/rate", response_class=HTMLResponse)
@limiter.limit(RATE_REVIEW)
async def rate_service(
    request: Request,
    slug: str,
    score: int = Form(...),
    comment: str = Form(""),
    reviewer_name: str = Form("Anonymous"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("Not Found", status_code=404)

    if score < 1:
        score = 1
    if score > 5:
        score = 5

    # SECURITY: Server-side length limits (HTML maxlength is trivially bypassed)
    if len(reviewer_name) > MAX_REVIEWER_NAME or len(comment) > MAX_COMMENT:
        return HTMLResponse("Input exceeds maximum length.", status_code=422)

    # Payment gate (skipped in test mode)
    if payments_enabled():
        payment_hash = request.query_params.get("payment_hash")
        if not payment_hash:
            invoice = await create_invoice(
                settings.AUTH_REVIEW_PRICE_SATS, "satring.com review submission"
            )
            form_fields = {
                "score": str(score),
                "comment": comment,
                "reviewer_name": reviewer_name,
            }
            html = templates.TemplateResponse(request, "services/_payment_widget.html", {
                "payment_hash": invoice["payment_hash"],
                "payment_request": invoice["payment_request"],
                "amount_sats": settings.AUTH_REVIEW_PRICE_SATS,
                "form_action": f"/services/{slug}/rate",
                "form_fields": form_fields,
                "htmx_mode": True,
                "slug": slug,
            })
            html.headers["HX-Retarget"] = "#payment-area"
            html.headers["HX-Reswap"] = "innerHTML"
            return html

        paid, paid_sats = await check_payment_status(payment_hash)
        if not paid or paid_sats < settings.AUTH_REVIEW_PRICE_SATS:
            return HTMLResponse(
                f'<div class="text-red-400 text-sm">Payment not verified or insufficient (need {settings.AUTH_REVIEW_PRICE_SATS} sats).</div>',
                status_code=402,
            )
        if not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse(
                '<div class="text-red-400 text-sm">Payment already used.</div>',
                status_code=402,
            )

    rating = Rating(
        service_id=service.id,
        score=score,
        comment=comment,
        reviewer_name=reviewer_name or "Anonymous",
    )
    db.add(rating)
    await db.flush()

    avg_result = await db.execute(
        select(func.avg(Rating.score), func.count(Rating.id))
        .where(Rating.service_id == service.id)
    )
    avg_row = avg_result.one()
    service.avg_rating = round(float(avg_row[0]), 1)
    service.rating_count = avg_row[1]
    await db.commit()

    return templates.TemplateResponse(request, "services/_review_bubble.html", {
        "rating": rating,
    })


@router.get("/services/{slug}/reputation-invoice", response_class=HTMLResponse)
@limiter.limit(RATE_INVOICE)
async def reputation_invoice(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    # Verify service exists
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    if not result.scalars().first():
        return HTMLResponse("Not Found", status_code=404)

    if not payments_enabled():
        # Test mode: skip invoice, go straight to result
        data = await build_reputation_data(db, slug)
        return templates.TemplateResponse(request, "services/_reputation_result.html", {"data": data})

    invoice = await create_invoice(
        settings.AUTH_REPUTATION_PRICE_SATS, "satring.com reputation report"
    )
    return templates.TemplateResponse(request, "services/_paid_report_widget.html", {
        "payment_hash": invoice["payment_hash"],
        "payment_request": invoice["payment_request"],
        "amount_sats": settings.AUTH_REPUTATION_PRICE_SATS,
        "result_url": f"/services/{slug}/reputation-result",
        "target_id": "reputation-area",
        "label": "reputation report",
    })


@router.get("/services/{slug}/reputation-result", response_class=HTMLResponse)
async def reputation_result(request: Request, slug: str, payment_hash: str = "", db: AsyncSession = Depends(get_db)):
    if payments_enabled():
        if not payment_hash:
            return HTMLResponse("Payment required", status_code=402)
        paid, paid_sats = await check_payment_status(payment_hash)
        if not paid or paid_sats < settings.AUTH_REPUTATION_PRICE_SATS:
            return HTMLResponse(
                f"Payment not verified or insufficient (need {settings.AUTH_REPUTATION_PRICE_SATS} sats).",
                status_code=402,
            )
        if not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse("Payment already used.", status_code=402)

    data = await build_reputation_data(db, slug)
    return templates.TemplateResponse(request, "services/_reputation_result.html", {"data": data})


@router.get("/services/{slug}/analytics-invoice", response_class=HTMLResponse)
@limiter.limit(RATE_INVOICE)
async def service_analytics_invoice(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    # Verify service exists
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    if not result.scalars().first():
        return HTMLResponse("Not Found", status_code=404)

    if not payments_enabled():
        data = await build_service_analytics(db, slug)
        return templates.TemplateResponse(request, "services/_service_analytics_result.html", {"data": data})

    invoice = await create_invoice(
        settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS, "satring.com service health report"
    )
    return templates.TemplateResponse(request, "services/_paid_report_widget.html", {
        "payment_hash": invoice["payment_hash"],
        "payment_request": invoice["payment_request"],
        "amount_sats": settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS,
        "result_url": f"/services/{slug}/analytics-result",
        "target_id": "analytics-area",
        "label": "health report",
    })


@router.get("/services/{slug}/analytics-result", response_class=HTMLResponse)
async def service_analytics_result(request: Request, slug: str, payment_hash: str = "", db: AsyncSession = Depends(get_db)):
    if payments_enabled():
        if not payment_hash:
            return HTMLResponse("Payment required", status_code=402)
        paid, paid_sats = await check_payment_status(payment_hash)
        if not paid or paid_sats < settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS:
            return HTMLResponse(
                f"Payment not verified or insufficient (need {settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS} sats).",
                status_code=402,
            )
        if not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse("Payment already used.", status_code=402)

    data = await build_service_analytics(db, slug)
    return templates.TemplateResponse(request, "services/_service_analytics_result.html", {"data": data})


@router.get("/analytics-invoice", response_class=HTMLResponse)
@limiter.limit(RATE_INVOICE)
async def analytics_invoice(request: Request, db: AsyncSession = Depends(get_db)):
    if not payments_enabled():
        data = await build_analytics_data(db)
        return templates.TemplateResponse(request, "services/_analytics_result.html", {"data": data})

    invoice = await create_invoice(
        settings.AUTH_ANALYTICS_PRICE_SATS, "satring.com analytics report"
    )
    return templates.TemplateResponse(request, "services/_paid_report_widget.html", {
        "payment_hash": invoice["payment_hash"],
        "payment_request": invoice["payment_request"],
        "amount_sats": settings.AUTH_ANALYTICS_PRICE_SATS,
        "result_url": "/analytics-result",
        "target_id": "analytics-area",
        "label": "directory analytics",
    })


@router.get("/analytics-result", response_class=HTMLResponse)
async def analytics_result(request: Request, payment_hash: str = "", db: AsyncSession = Depends(get_db)):
    if payments_enabled():
        if not payment_hash:
            return HTMLResponse("Payment required", status_code=402)
        paid, paid_sats = await check_payment_status(payment_hash)
        if not paid or paid_sats < settings.AUTH_ANALYTICS_PRICE_SATS:
            return HTMLResponse(
                f"Payment not verified or insufficient (need {settings.AUTH_ANALYTICS_PRICE_SATS} sats).",
                status_code=402,
            )
        if not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse("Payment already used.", status_code=402)

    data = await build_analytics_data(db)
    return templates.TemplateResponse(request, "services/_analytics_result.html", {"data": data})


@router.get("/payment-status/{payment_hash}")
@limiter.limit(RATE_PAYMENT_STATUS)
async def payment_status(request: Request, payment_hash: str):
    if not payments_enabled():
        return JSONResponse({"paid": True})
    paid, _ = await check_payment_status(payment_hash)
    return JSONResponse({"paid": paid})


@router.get("/")
async def stats_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Free public stats page with high-level directory metrics.
    Returns JSON manifest when Accept: application/json is requested."""
    from datetime import timedelta
    now = utc_now()

    # Total services (non-purged)
    total = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged")
    )).scalar() or 0

    # By status (include all types)
    all_status_rows = (await db.execute(
        select(Service.status, func.count(Service.id))
        .group_by(Service.status)
    )).all()
    by_status = {r[0]: r[1] for r in all_status_rows}
    confirmed_count = by_status.get("confirmed", 0)
    live_count = by_status.get("live", 0)
    unhealthy = by_status.get("down", 0) + by_status.get("unknown", 0)
    healthy_count = total - unhealthy
    healthy_pct = (healthy_count / total * 100) if total else 0

    # By protocol: aggregate combo protocols into base components
    # e.g. "L402+x402" counts toward both L402 and x402 totals; "L402+MPP" toward L402 and MPP
    proto_rows = (await db.execute(
        select(Service.protocol, func.count(Service.id),
               func.sum(case((Service.status == "confirmed", 1), else_=0)),
               func.sum(case((Service.status == "live", 1), else_=0)),
               func.sum(case((Service.domain_verified == True, 1), else_=0)))
        .where(Service.status != "purged")
        .group_by(Service.protocol)
    )).all()
    base_counts: dict[str, dict] = {}
    for proto, count, confirmed, live, verified in proto_rows:
        for base in proto.split("+"):
            if base not in base_counts:
                base_counts[base] = {"count": 0, "confirmed": 0, "live": 0, "verified": 0}
            base_counts[base]["count"] += count
            base_counts[base]["confirmed"] += (confirmed or 0)
            base_counts[base]["live"] += (live or 0)
            base_counts[base]["verified"] += (verified or 0)
    by_protocol = []
    for base in ("L402", "x402", "MPP"):
        if base in base_counts:
            c = base_counts[base]
            health_pct = ((c["confirmed"] + c["live"]) / c["count"] * 100) if c["count"] else 0
            by_protocol.append({"protocol": base, "count": c["count"], "confirmed": c["confirmed"], "live": c["live"], "verified": c["verified"], "health_pct": health_pct})

    # Domain verified
    verified_count = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged", Service.domain_verified == True)
    )).scalar() or 0

    # Total ratings
    total_ratings = (await db.execute(select(func.count(Rating.id)))).scalar() or 0

    # By category
    cat_rows = (await db.execute(
        select(Category.name, Category.slug,
               func.count(Service.id),
               func.sum(case((Service.status == "confirmed", 1), else_=0)),
               func.sum(case((Service.status == "live", 1), else_=0)),
               func.sum(case((Service.domain_verified == True, 1), else_=0)),
               func.avg(Service.avg_rating))
        .join(service_categories, Category.id == service_categories.c.category_id)
        .join(Service, Service.id == service_categories.c.service_id)
        .where(Service.status != "purged")
        .group_by(Category.id)
        .order_by(func.count(Service.id).desc())
    )).all()
    categories = [{"name": r[0], "slug": r[1], "count": r[2], "confirmed": r[3] or 0, "live": r[4] or 0, "verified": r[5] or 0, "avg_rating": r[6] or 0} for r in cat_rows]

    # Growth
    added_7d = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged", Service.created_at >= now - timedelta(days=7))
    )).scalar() or 0
    added_30d = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged", Service.created_at >= now - timedelta(days=30))
    )).scalar() or 0
    ratings_7d = (await db.execute(
        select(func.count(Rating.id)).where(Rating.created_at >= now - timedelta(days=7))
    )).scalar() or 0
    ratings_30d = (await db.execute(
        select(func.count(Rating.id)).where(Rating.created_at >= now - timedelta(days=30))
    )).scalar() or 0

    newest = (await db.execute(
        select(Service).where(Service.status != "purged").order_by(Service.created_at.desc()).limit(1)
    )).scalars().first()

    # Top rated (min 2 reviews)
    top_rated = (await db.execute(
        select(Service).where(Service.status != "purged", Service.rating_count >= 2)
        .order_by(Service.avg_rating.desc(), Service.rating_count.desc()).limit(5)
    )).scalars().all()

    # Recently added
    recently_added = (await db.execute(
        select(Service).where(Service.status != "purged").order_by(Service.created_at.desc()).limit(5)
    )).scalars().all()

    # JSON content negotiation for agents
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        base = settings.BASE_URL.rstrip("/")
        return JSONResponse({
            "name": "satring",
            "description": "Curated paid API directory for AI agents. L402, x402, and MPP services.",
            "url": base,
            "api": f"{base}/api/v1/",
            "openapi": f"{base}/openapi.json",
            "docs": f"{base}/docs",
            "llms_txt": f"{base}/llms.txt",
            "protocols": ["L402", "x402", "MPP"],
            "discovery": {
                "agent": f"{base}/.well-known/agent.json",
                "l402": f"{base}/.well-known/l402",
                "x402": f"{base}/.well-known/x402",
                "mpp": f"{base}/.well-known/mpp",
            },
            "stats": {
                "total_services": total,
                "live": live_count,
                "confirmed": confirmed_count,
                "verified": verified_count,
                "total_ratings": total_ratings,
                "protocols": {p["protocol"]: p["count"] for p in by_protocol},
                "added_7d": added_7d,
                "added_30d": added_30d,
            },
        })

    return templates.TemplateResponse(request, "services/stats.html", {
        "stats": {
            "total_services": total,
            "confirmed_count": confirmed_count,
            "live_count": live_count,
            "total_ratings": total_ratings,
            "verified_count": verified_count,
            "by_status": by_status,
            "healthy_pct": healthy_pct,
            "by_protocol": by_protocol,
            "categories": categories,
            "added_7d": added_7d,
            "added_30d": added_30d,
            "ratings_7d": ratings_7d,
            "ratings_30d": ratings_30d,
            "newest": newest,
            "top_rated": top_rated,
            "recently_added": recently_added,
        },
    })


@router.get("/sitemap.xml")
@limiter.limit(RATE_SITEMAP)
async def sitemap(request: Request, db: AsyncSession = Depends(get_db)):
    from fastapi.responses import Response
    base = settings.BASE_URL.rstrip("/")

    urls = [
        f'  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>',
        f'  <url><loc>{base}/directory</loc><changefreq>daily</changefreq><priority>0.9</priority></url>',
        f'  <url><loc>{base}/submit</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>',
        f'  <url><loc>{base}/docs</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>',
        f'  <url><loc>{base}/robots.txt</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>',
        f'  <url><loc>{base}/llms.txt</loc><changefreq>weekly</changefreq><priority>0.4</priority></url>',
    ]

    # SECURITY: Individual service slugs removed from sitemap to prevent
    # single-request enumeration of the entire catalog. Services are still
    # discoverable via /directory page links and search engine crawling.

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += '\n'.join(urls)
    xml += '\n</urlset>'
    return Response(content=xml, media_type="application/xml")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html")


@router.get("/robots.txt")
async def robots_txt():
    base = settings.BASE_URL.rstrip("/")
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /services/*/meta.json\n"
        "Crawl-delay: 30\n"
        "\n"
        "User-agent: AhrefsBot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: SemrushBot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: MJ12bot\n"
        "Disallow: /\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
        f"Llms-txt: {base}/llms.txt\n"
    )
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content)


@router.get("/llms.txt")
async def llms_txt():
    from fastapi.responses import PlainTextResponse
    from app.main import SEED_CATEGORIES
    base = settings.BASE_URL.rstrip("/")

    lines = [
        "# satring",
        "",
        "> Curated paid API directory for AI agents. L402 + x402 + MPP.",
        "",
        "Satring is a curated directory of paid API services: APIs that accept payments via Lightning (L402), USDC on Base (x402), or Stripe/Tempo (MPP).",
        "AI agents use satring to find and connect to paid APIs autonomously via MCP.",
        "Humans use it to discover, rate, and submit services.",
        "",
        f"- [Stats]({base}/): Live directory metrics, protocol breakdown, category coverage",
        f"- [Browse directory]({base}/directory): Search and filter services by category, status, protocol, rating",
        f"- [Submit a service]({base}/submit): List your paid API (L402, MPP, or x402 payment required)",
        f"- [API docs]({base}/docs): OpenAPI/Swagger interactive documentation",
        f"- [JSON API]({base}/api/v1/services): Programmatic access to the catalog",
        "",
        "## API",
        "",
        "### Free tier (5 summaries/day per IP)",
        "Free endpoints return thin summaries: name, protocol, price, rating, categories.",
        "Summaries do NOT include service URLs, descriptions, or payment config.",
        "",
        f"- [List services]({base}/api/v1/services): GET — paginated summaries, filterable by category, status, protocol",
        f"- [Search]({base}/api/v1/search?q=example): GET — search summaries across names",
        f"- [Service detail]({base}/api/v1/services/{{slug}}): GET — service summary",
        f"- [Ratings]({base}/api/v1/services/{{slug}}/ratings): GET — paginated reviews (no quota)",
        "",
        "### Paid endpoints (full metadata with URLs)",
        "Pay via L402 (Lightning), MPP (Lightning), or x402 (USDC on Base) to access full service data.",
        "",
        f"- [Bulk export]({base}/api/v1/services/bulk): GET — all services with full metadata including URLs",
        f"- [Submit service]({base}/api/v1/services): POST — create a new listing",
        f"- [Submit rating]({base}/api/v1/services/{{slug}}/ratings): POST — rate a service",
        f"- [Analytics]({base}/api/v1/analytics): GET — aggregate directory stats",
        f"- [Reputation]({base}/api/v1/services/{{slug}}/reputation): GET — detailed service reputation report",
        "",
        "### Owner endpoints (requires X-Edit-Token header)",
        "Service owners can view aggregated traffic for their domain's listings.",
        "",
        f"- [Owner traffic]({base}/api/v1/owner/{{domain}}/traffic): GET — free, aggregated hits across all domain services",
        f"- [Owner audience]({base}/api/v1/owner/{{domain}}/audience): GET — paid, detailed audience analytics",
        f"- [Owner dashboard]({base}/owner/{{domain}}?token=YOUR_TOKEN): Web UI dashboard",
        "",
        "## Categories",
        "",
    ]
    for name, slug, description in SEED_CATEGORIES:
        lines.append(f"- [{name}]({base}/directory?category={slug}): {description}")

    lines.extend([
        "",
        "## How to Pay",
        "",
        "All paid endpoints return HTTP 402 with payment challenges in headers.",
        "Three payment methods are supported (pick any one):",
        "",
        "### L402 (Lightning)",
        "1. Hit a paid endpoint; receive 402 with `WWW-Authenticate: L402 macaroon=\"...\", invoice=\"...\"`",
        "2. Pay the BOLT11 Lightning invoice to get the preimage",
        "3. Retry the request with `Authorization: L402 <macaroon>:<preimage>`",
        f"- Discovery: {base}/.well-known/l402",
        "",
        "### MPP (Machine Payments Protocol)",
        "1. Hit a paid endpoint; receive 402 with `WWW-Authenticate: Payment ...`",
        "2. Parse the challenge, pay the BOLT11 invoice to get the preimage",
        "3. Build a credential JSON with paymentHash and preimage, base64url-encode it",
        "4. Retry with `Authorization: Payment <base64url-credential>`",
        f"- Discovery: {base}/.well-known/mpp",
        "",
        "### x402 (USDC on Base)",
        "1. Hit a paid endpoint; receive 402 with `PAYMENT-REQUIRED` header (base64 JSON)",
        "2. Decode to get payTo address, amount, and network (Base mainnet)",
        "3. Send USDC, then retry with `PAYMENT-SIGNATURE` header",
        f"- Discovery: {base}/.well-known/x402",
        "",
    ])
    return PlainTextResponse("\n".join(lines), media_type="text/plain")


# --- Well-known protocol discovery routes ---
# These are dynamic so they stay in sync with env config.


@router.get("/.well-known/x402")
async def well_known_x402():
    """x402 payment discovery endpoint."""
    from app.config import x402_enabled
    base = settings.BASE_URL.rstrip("/")
    data = {
        "protocol": "x402",
        "version": "2",
        "enabled": x402_enabled(),
        "facilitator": settings.X402_FACILITATOR_URL,
        "network": settings.X402_NETWORK,
        "asset": settings.X402_ASSET,
        "payTo": settings.X402_PAY_TO or None,
        "endpoints": {
            "submit": f"{base}/api/v1/services",
            "bulk": f"{base}/api/v1/services/bulk",
            "analytics": f"{base}/api/v1/analytics",
        },
        "pricing": {
            "submit": settings.AUTH_SUBMIT_PRICE_USD,
            "bulk": settings.AUTH_BULK_PRICE_USD,
            "analytics": settings.AUTH_ANALYTICS_PRICE_USD,
            "reputation": settings.AUTH_REPUTATION_PRICE_USD,
        },
        "example": {
            "description": "Fetch bulk export with x402 USDC payment (Python)",
            "code": (
                "import httpx, json, base64\n"
                "# 1. Hit endpoint to get 402 with PAYMENT-REQUIRED header\n"
                f"r = httpx.get('{base}/api/v1/services/bulk')\n"
                "payment_info = json.loads(base64.b64decode(r.headers['PAYMENT-REQUIRED']))\n"
                "# 2. Send USDC to payTo address on Base via your wallet/SDK\n"
                "# 3. Get signature from x402 facilitator or build PAYMENT-SIGNATURE\n"
                "# 4. Retry with signature header\n"
                f"r = httpx.get('{base}/api/v1/services/bulk',\n"
                "    headers={{'PAYMENT-SIGNATURE': signature_b64}}\n"
                ")"
            ),
        },
        "docs": f"{base}/docs",
    }
    return JSONResponse(data)


@router.get("/.well-known/l402")
async def well_known_l402():
    """L402 Lightning payment discovery endpoint."""
    base = settings.BASE_URL.rstrip("/")
    data = {
        "protocol": "L402",
        "description": "Lightning-native HTTP 402 payments. Pay invoice, get preimage, authenticate with macaroon.",
        "auth_scheme": "L402",
        "auth_header_format": "Authorization: L402 <macaroon>:<preimage>",
        "endpoints": {
            "submit": f"{base}/api/v1/services",
            "bulk": f"{base}/api/v1/services/bulk",
            "analytics": f"{base}/api/v1/analytics",
        },
        "pricing_sats": {
            "submit": settings.AUTH_SUBMIT_PRICE_SATS,
            "review": settings.AUTH_REVIEW_PRICE_SATS,
            "bulk": settings.AUTH_BULK_PRICE_SATS,
            "analytics": settings.AUTH_ANALYTICS_PRICE_SATS,
            "reputation": settings.AUTH_REPUTATION_PRICE_SATS,
        },
        "example": {
            "description": "Fetch bulk export with L402 payment (Python)",
            "code": (
                "import httpx, hashlib\n"
                "# 1. Hit endpoint to get 402 challenge\n"
                f"r = httpx.get('{base}/api/v1/services/bulk')\n"
                "www_auth = r.headers['WWW-Authenticate']\n"
                "macaroon = www_auth.split('macaroon=\"')[1].split('\"')[0]\n"
                "invoice = www_auth.split('invoice=\"')[1].split('\"')[0]\n"
                "# 2. Pay invoice via your Lightning wallet, get preimage\n"
                "preimage = pay_invoice(invoice)  # your wallet SDK\n"
                "# 3. Retry with L402 auth\n"
                f"r = httpx.get('{base}/api/v1/services/bulk',\n"
                "    headers={{'Authorization': f'L402 {{macaroon}}:{{preimage}}'}}\n"
                ")\n"
                "services = r.json()  # full catalog with URLs"
            ),
        },
        "docs": f"{base}/docs",
    }
    return JSONResponse(data)


@router.get("/.well-known/mpp")
async def well_known_mpp():
    """MPP (Machine Payments Protocol) discovery endpoint."""
    base = settings.BASE_URL.rstrip("/")
    data = {
        "protocol": "MPP",
        "version": "draft-httpauth-payment-00",
        "description": "Machine Payments Protocol with Lightning charge method. HMAC-bound challenges, stateless verification.",
        "auth_scheme": "Payment",
        "auth_header_format": "Authorization: Payment <base64url-credential>",
        "charge_method": "lightning",
        "currency": "BTC",
        "endpoints": {
            "submit": f"{base}/api/v1/services",
            "bulk": f"{base}/api/v1/services/bulk",
            "analytics": f"{base}/api/v1/analytics",
        },
        "pricing_sats": {
            "submit": settings.AUTH_SUBMIT_PRICE_SATS,
            "review": settings.AUTH_REVIEW_PRICE_SATS,
            "bulk": settings.AUTH_BULK_PRICE_SATS,
            "analytics": settings.AUTH_ANALYTICS_PRICE_SATS,
            "reputation": settings.AUTH_REPUTATION_PRICE_SATS,
        },
        "example": {
            "description": "Fetch bulk export with MPP payment (Python)",
            "code": (
                "import httpx, json, base64\n"
                "# 1. Hit endpoint to get 402 with Payment challenge\n"
                f"r = httpx.get('{base}/api/v1/services/bulk')\n"
                "# 2. Parse Payment challenge from WWW-Authenticate\n"
                "# 3. Pay the BOLT11 invoice, get preimage\n"
                "preimage = pay_invoice(invoice)  # your wallet SDK\n"
                "# 4. Build credential: {{\"paymentHash\": ..., \"preimage\": ...}}\n"
                "cred = base64.urlsafe_b64encode(json.dumps(\n"
                "    {{'paymentHash': payment_hash, 'preimage': preimage}}\n"
                ").encode()).decode()\n"
                "# 5. Retry with Payment auth\n"
                f"r = httpx.get('{base}/api/v1/services/bulk',\n"
                "    headers={{'Authorization': f'Payment {{cred}}'}}\n"
                ")"
            ),
        },
        "docs": f"{base}/docs",
    }
    return JSONResponse(data)
