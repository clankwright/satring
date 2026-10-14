"""Paid submissions publish without a follow-up POST."""

from contextlib import asynccontextmanager

import pytest
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PendingSubmission, Service
from app.submissions import publish_due_submissions


def _form(name="Bascule Press", url="https://basculepress.example/.well-known/l402-services"):
    from urllib.parse import urlencode
    return urlencode({
        "name": name,
        "url": url,
        "description": "Translations sold for bitcoin",
        "protocol": "L402",
        "pricing_sats": "4000",
        "pricing_model": "flat",
        "owner_name": "Bascule Press",
        "owner_contact": "orders@basculepress.example",
        "categories": "9",
    })


@pytest.mark.asyncio
async def test_poll_publishes_stored_submission(client: AsyncClient, db: AsyncSession):
    payment_hash = "a" * 64
    with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
         patch("app.routes.web.create_invoice", new_callable=AsyncMock) as mock_inv, \
         patch("app.routes.web.check_payment_status", new_callable=AsyncMock) as mock_chk, \
         patch("app.routes.web.send_verify_email") as mock_email:
        mock_inv.return_value = {
            "payment_hash": payment_hash,
            "payment_request": "lnbc1pending",
        }
        mock_chk.return_value = (True, settings.AUTH_SUBMIT_PRICE_SATS)

        first = await client.post(
            "/submit",
            content=_form(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert first.status_code == 200
        assert "Payment Required" in first.text
        pending = await db.get(PendingSubmission, payment_hash)
        assert pending is not None
        assert pending.service_id is None

        status = await client.get(f"/payment-status/{payment_hash}")
        assert status.status_code == 200
        assert status.json() == {"paid": True}

        services = (await db.execute(
            select(Service).where(Service.name == "Bascule Press")
        )).scalars().all()
        assert len(services) == 1
        service = services[0]
        assert service.url == "https://basculepress.example/.well-known/l402-services"
        assert service.pricing_sats == 4000
        assert service.pricing_model == "flat"
        assert service.protocol == "L402"
        assert service.owner_contact == "orders@basculepress.example"
        again = await client.get(f"/payment-status/{payment_hash}")
        assert again.json() == {"paid": True}
        count = (await db.execute(
            select(func.count()).select_from(Service).where(Service.name == "Bascule Press")
        )).scalar()
        assert count == 1

        # The payment page's follow-up POST must show success, not "already used".
        follow = await client.post(
            f"/submit?payment_hash={payment_hash}",
            content=_form(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert follow.status_code == 200
        assert "Service Submitted" in follow.text
        assert service.slug in follow.text
        mock_email.assert_called_once()


@pytest.mark.asyncio
async def test_unpaid_poll_does_not_publish(client: AsyncClient, db: AsyncSession):
    payment_hash = "b" * 64
    with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
         patch("app.routes.web.create_invoice", new_callable=AsyncMock) as mock_inv, \
         patch("app.routes.web.check_payment_status", new_callable=AsyncMock) as mock_chk:
        mock_inv.return_value = {"payment_hash": payment_hash, "payment_request": "lnbc1nope"}
        mock_chk.return_value = (False, 0)
        await client.post(
            "/submit",
            content=_form(name="Unpaid Press", url="https://unpaid.example/x"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = await client.get(f"/payment-status/{payment_hash}")
        assert status.json() == {"paid": False}
        found = (await db.execute(select(Service).where(Service.name == "Unpaid Press"))).scalars().first()
        assert found is None


@pytest.mark.asyncio
async def test_short_payment_does_not_publish(client: AsyncClient, db: AsyncSession):
    payment_hash = "c" * 64
    with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
         patch("app.routes.web.create_invoice", new_callable=AsyncMock) as mock_inv, \
         patch("app.routes.web.check_payment_status", new_callable=AsyncMock) as mock_chk:
        mock_inv.return_value = {"payment_hash": payment_hash, "payment_request": "lnbc1short"}
        mock_chk.return_value = (True, 1)
        await client.post(
            "/submit",
            content=_form(name="Short Pay", url="https://short.example/x"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = await client.get(f"/payment-status/{payment_hash}")
        assert status.json() == {"paid": True}
        found = (await db.execute(select(Service).where(Service.name == "Short Pay"))).scalars().first()
        assert found is None


@pytest.mark.asyncio
async def test_sweeper_publishes_paid_pending(client: AsyncClient, db: AsyncSession):
    payment_hash = "d" * 64
    with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
         patch("app.routes.web.create_invoice", new_callable=AsyncMock) as mock_inv, \
         patch("app.submissions.check_payment_status", new_callable=AsyncMock) as mock_chk, \
         patch("app.submissions.send_verify_email"):
        mock_inv.return_value = {"payment_hash": payment_hash, "payment_request": "lnbc1sweep"}
        mock_chk.return_value = (True, settings.AUTH_SUBMIT_PRICE_SATS)
        await client.post(
            "/submit",
            content=_form(name="Swept Press", url="https://swept.example/x"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        @asynccontextmanager
        async def _factory():
            yield db

        created = await publish_due_submissions(session_factory=_factory)
        assert created == 1
        found = (await db.execute(select(Service).where(Service.name == "Swept Press"))).scalars().one()
        assert found.pricing_model == "flat"

        created_again = await publish_due_submissions(session_factory=_factory)
        assert created_again == 0
