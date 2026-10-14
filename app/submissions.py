"""Publish a paid directory submission.

The submit form creates a Lightning invoice and stores the form payload
against that payment hash. The listing is written when the invoice is paid:
either by the follow-up POST from the payment page, or by a short background
loop for clients that pay and only check payment status.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    PENDING_PUBLISH_INTERVAL,
    PENDING_PUBLISH_MAX_AGE_HOURS,
    settings,
)
from app.database import async_session
from app.l402 import check_payment_status
from app.models import Category, ConsumedPayment, PendingSubmission, Service
from app.utils import (
    extract_domain,
    extract_email,
    find_existing_service,
    find_purged_service,
    generate_edit_token,
    get_same_domain_services,
    hash_token,
    overwrite_purged_service,
    send_verify_email,
    unique_slug,
    utc_now,
    verify_edit_token,
)

logger = logging.getLogger("satring.submissions")

_publish_task: asyncio.Task | None = None


@dataclass
class PublishResult:
    paid: bool
    service: Service | None = None
    edit_token: str = ""
    token_reused: bool = False
    created: bool = False
    blocked: bool = False


def _postgres(db: AsyncSession) -> bool:
    bind = db.bind
    return bool(bind is not None and bind.dialect.name == "postgresql")


async def store_pending_submission(db: AsyncSession, payment_hash: str, payload: dict) -> None:
    db.add(PendingSubmission(payment_hash=payment_hash, payload=json.dumps(payload)))
    await db.commit()


async def _materialize(db: AsyncSession, payload: dict) -> tuple[Service, str, bool]:
    name = payload["name"]
    url = payload["url"]
    description = payload.get("description") or ""
    protocol = payload.get("protocol") or "L402"
    pricing_sats = int(payload.get("pricing_sats") or 0)
    pricing_model = payload.get("pricing_model") or "per-request"
    owner_name = payload.get("owner_name") or ""
    owner_contact = payload.get("owner_contact") or ""
    logo_url = payload.get("logo_url") or ""
    category_ids = [int(c) for c in payload.get("category_ids") or []]
    existing_edit_token = payload.get("existing_edit_token") or ""
    x402_network = payload.get("x402_network") or None
    x402_asset = payload.get("x402_asset") or None
    x402_pay_to = payload.get("x402_pay_to") or None
    pricing_usd = payload.get("pricing_usd") or None
    mpp_method = payload.get("mpp_method") or None
    mpp_realm = payload.get("mpp_realm") or None
    mpp_currency = payload.get("mpp_currency") or None

    slug = await unique_slug(db, name)
    domain_services = await get_same_domain_services(db, url)

    token_reused = False
    edit_token_hash = None
    edit_token = ""
    if existing_edit_token:
        for ds in domain_services:
            if ds.edit_token_hash and verify_edit_token(existing_edit_token, ds.edit_token_hash):
                token_reused = True
                edit_token = existing_edit_token
                edit_token_hash = ds.edit_token_hash
                break
    if not token_reused:
        edit_token = generate_edit_token()
        edit_token_hash = hash_token(edit_token)

    auto_verified = False
    inherited_challenge = None
    for ds in domain_services:
        if ds.domain_verified:
            auto_verified = True
            inherited_challenge = ds.domain_challenge
            break

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
            x402_network=x402_network, x402_asset=x402_asset,
            x402_pay_to=x402_pay_to, pricing_usd=pricing_usd,
            mpp_method=mpp_method, mpp_realm=mpp_realm,
            mpp_currency=mpp_currency,
        )
        service = purged
    else:
        service = Service(
            name=name, slug=slug, url=url, description=description,
            protocol=protocol, pricing_sats=pricing_sats, pricing_model=pricing_model,
            owner_name=owner_name, owner_contact=owner_contact, logo_url=logo_url,
            x402_network=x402_network, x402_asset=x402_asset,
            x402_pay_to=x402_pay_to, pricing_usd=pricing_usd,
            mpp_method=mpp_method, mpp_realm=mpp_realm,
            mpp_currency=mpp_currency,
            edit_token_hash=edit_token_hash,
            domain_verified=auto_verified,
            domain_challenge=inherited_challenge,
        )
        if category_ids:
            cats = (await db.execute(
                select(Category).where(Category.id.in_(category_ids))
            )).scalars().all()
            service.categories = list(cats)
        db.add(service)
    await db.flush()
    return service, edit_token, token_reused


def _already(pending: PendingSubmission, service: Service | None) -> PublishResult:
    return PublishResult(
        paid=True,
        service=service,
        edit_token=pending.edit_token or "",
        token_reused=bool(pending.token_reused),
        created=False,
    )


async def publish_pending_submission(
    db: AsyncSession,
    payment_hash: str,
    payment: tuple | None = None,
) -> PublishResult | None:
    """Create the listing for a stored submission if its invoice is paid.

    Returns None when this hash has no stored submission (legacy recovery
    still builds the listing from the follow-up form).
    """
    result = await db.execute(
        select(PendingSubmission).where(PendingSubmission.payment_hash == payment_hash)
    )
    pending = result.scalar_one_or_none()
    if pending is None:
        return None
    if pending.service_id:
        service = await db.get(Service, pending.service_id)
        return _already(pending, service)
    if pending.error:
        return PublishResult(paid=True, blocked=True)

    if payment is None:
        payment = await check_payment_status(payment_hash)
    paid, paid_sats = payment
    try:
        paid_sats = int(paid_sats or 0)
    except (TypeError, ValueError):
        paid_sats = 0
    if not paid or paid_sats < settings.AUTH_SUBMIT_PRICE_SATS:
        return PublishResult(paid=False)

    stmt = select(PendingSubmission).where(PendingSubmission.payment_hash == payment_hash)
    if _postgres(db):
        stmt = stmt.with_for_update()
    locked = await db.execute(stmt)
    pending = locked.scalar_one()
    if pending.service_id:
        service = await db.get(Service, pending.service_id)
        return _already(pending, service)
    if pending.error:
        return PublishResult(paid=True, blocked=True)

    payload = json.loads(pending.payload)
    url = payload.get("url") or ""
    existing = await find_existing_service(db, url)
    if existing:
        pending.error = "duplicate url"
        await db.commit()
        logger.error(
            "PENDING blocked duplicate url=%r hash=%s existing=%s",
            url, payment_hash, existing.slug,
        )
        return PublishResult(paid=True, blocked=True)

    service, edit_token, token_reused = await _materialize(db, payload)
    try:
        db.add(ConsumedPayment(payment_hash=payment_hash))
        await db.flush()
    except IntegrityError:
        await db.rollback()
        again = await db.execute(
            select(PendingSubmission).where(PendingSubmission.payment_hash == payment_hash)
        )
        pending = again.scalar_one_or_none()
        if pending and pending.service_id:
            service = await db.get(Service, pending.service_id)
            return _already(pending, service)
        logger.error("PENDING payment already consumed hash=%s", payment_hash)
        return PublishResult(paid=True)

    pending.service_id = service.id
    pending.edit_token = edit_token
    pending.token_reused = token_reused
    pending.published_at = utc_now()
    await db.commit()
    logger.info(
        "SUBMIT PUBLISHED slug=%s id=%s url=%r hash=%s",
        service.slug, service.id, service.url, payment_hash,
    )
    return PublishResult(
        paid=True,
        service=service,
        edit_token=edit_token,
        token_reused=token_reused,
        created=True,
    )


def _queue_verify_email(service: Service) -> None:
    if service.domain_verified:
        return
    email = extract_email(service.owner_contact or "")
    if not email:
        return
    domain = extract_domain(service.url)
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, send_verify_email, email, service.slug, domain)
    except RuntimeError:
        send_verify_email(email, service.slug, domain)


async def publish_due_submissions(session_factory=None) -> int:
    """Publish every stored submission whose invoice has settled. Returns how many were created."""
    factory = session_factory or async_session
    cutoff = utc_now() - timedelta(hours=PENDING_PUBLISH_MAX_AGE_HOURS)
    async with factory() as db:
        rows = (await db.execute(
            select(PendingSubmission.payment_hash)
            .where(PendingSubmission.service_id.is_(None))
            .where(PendingSubmission.error.is_(None))
            .where(PendingSubmission.created_at >= cutoff)
        )).scalars().all()

    created = 0
    for payment_hash in rows:
        async with factory() as db:
            try:
                result = await publish_pending_submission(db, payment_hash)
            except Exception:
                logger.exception("PENDING publish failed hash=%s", payment_hash)
                continue
        if result and result.created and result.service is not None:
            created += 1
            _queue_verify_email(result.service)
    if created:
        logger.info("Pending publish cycle created %s listing(s)", created)
    return created


async def _publish_loop() -> None:
    try:
        while True:
            await asyncio.sleep(PENDING_PUBLISH_INTERVAL)
            try:
                await publish_due_submissions()
            except Exception:
                logger.exception("Pending submission publish failed")
    except asyncio.CancelledError:
        pass


def start_publish_task() -> None:
    global _publish_task
    _publish_task = asyncio.create_task(_publish_loop())


async def stop_publish_task() -> None:
    global _publish_task
    if _publish_task:
        _publish_task.cancel()
        try:
            await _publish_task
        except asyncio.CancelledError:
            pass
        _publish_task = None
