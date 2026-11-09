"""Integration tests for app/payment.py: multi-protocol payment gate."""

import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch, AsyncMock

from app.config import settings
from app.l402 import mint_macaroon
from app.main import app, limiter
from app.database import get_db

from tests.conftest import _make_db, _teardown_db


MOCK_INVOICE = {
    "payment_hash": "mockhash" * 8,
    "payment_request": "lnbc1000n1mock",
}


@pytest_asyncio.fixture
async def payment_client():
    """Client fixture for payment tests with test-mode enabled."""
    engine, session = await _make_db()

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    limiter.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    await _teardown_db(engine, session)


def _make_l402_token(preimage_bytes: bytes) -> tuple[str, str]:
    """Create a valid L402 auth header value from a preimage.

    Returns (auth_header_value, payment_hash).
    """
    preimage_hex = preimage_bytes.hex()
    payment_hash = hashlib.sha256(preimage_bytes).hexdigest()
    mac_b64 = mint_macaroon(payment_hash)
    return f"L402 {mac_b64}:{preimage_hex}", payment_hash


class TestPaymentRouting:
    """Test that require_payment routes to correct protocol based on headers."""

    @pytest.mark.asyncio
    async def test_test_mode_bypass(self, payment_client):
        """In test mode, all payment gates are bypassed."""
        assert settings.AUTH_ROOT_KEY == "test-mode"
        # Analytics endpoint should work without any auth headers
        resp = await payment_client.get("/api/v1/analytics")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_bulk_export_no_auth_test_mode(self, payment_client):
        """Bulk export works in test mode without auth."""
        resp = await payment_client.get("/api/v1/services/bulk")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_reputation_test_mode(self, payment_client):
        """Reputation endpoint works in test mode (creates service first)."""
        from sqlalchemy import select
        # Create a service directly via API
        resp = await payment_client.post("/api/v1/services", json={
            "name": "Payment Test Service",
            "url": "https://payment-test.example.com",
            "category_ids": [1],
        })
        assert resp.status_code == 201
        slug = resp.json()["slug"]

        resp = await payment_client.get(f"/api/v1/services/{slug}/reputation")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_create_rating_test_mode(self, payment_client):
        """Rating creation works in test mode."""
        # Create service first
        resp = await payment_client.post("/api/v1/services", json={
            "name": "Rating Test Service",
            "url": "https://rating-test.example.com",
            "category_ids": [1],
        })
        assert resp.status_code == 201
        slug = resp.json()["slug"]

        resp = await payment_client.post(f"/api/v1/services/{slug}/ratings", json={
            "score": 5,
            "comment": "Great service",
        })
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# L402 replay protection
# ---------------------------------------------------------------------------

class TestL402ReplayProtection:
    """Reusing the same L402 token for multiple paid actions must be blocked."""

    @pytest.mark.asyncio
    async def test_submit_replay_blocked(self, payment_client):
        """Second service submission with the same L402 token gets 402."""
        with patch.object(settings, "AUTH_ROOT_KEY", "replay-test-key"), \
             patch("app.payment.create_invoice", new_callable=AsyncMock) as mock_inv, \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_l402_inv, \
             patch("app.l402.check_payment_status", new_callable=AsyncMock) as mock_status:
            mock_inv.return_value = MOCK_INVOICE
            mock_l402_inv.return_value = MOCK_INVOICE
            # Legit payment: invoice settled for at least the endpoint price.
            # Lets these tests isolate replay logic from the amount check.
            mock_status.return_value = (True, 1_000_000)

            auth_header, _ = _make_l402_token(b"submit-replay-preimage")

            resp1 = await payment_client.post("/api/v1/services", json={
                "name": "Legit Paid Service",
                "url": "https://legit-paid.example.com",
                "category_ids": [1],
            }, headers={"Authorization": auth_header})
            assert resp1.status_code == 201

            resp2 = await payment_client.post("/api/v1/services", json={
                "name": "Replay Attack Service",
                "url": "https://replay-attack.example.com",
                "category_ids": [1],
            }, headers={"Authorization": auth_header})
            assert resp2.status_code == 402
            assert "already consumed" in resp2.json()["detail"]

    @pytest.mark.asyncio
    async def test_rating_replay_blocked(self, payment_client):
        """Second rating with the same L402 token gets 402."""
        # Create service in test mode
        resp = await payment_client.post("/api/v1/services", json={
            "name": "Rating Replay Target",
            "url": "https://rating-replay-target.example.com",
            "category_ids": [1],
        })
        assert resp.status_code == 201
        slug = resp.json()["slug"]

        with patch.object(settings, "AUTH_ROOT_KEY", "replay-test-key"), \
             patch("app.payment.create_invoice", new_callable=AsyncMock) as mock_inv, \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_l402_inv, \
             patch("app.l402.check_payment_status", new_callable=AsyncMock) as mock_status:
            mock_inv.return_value = MOCK_INVOICE
            mock_l402_inv.return_value = MOCK_INVOICE
            # Legit payment: invoice settled for at least the endpoint price.
            # Lets these tests isolate replay logic from the amount check.
            mock_status.return_value = (True, 1_000_000)

            auth_header, _ = _make_l402_token(b"rating-replay-preimage")

            resp1 = await payment_client.post(f"/api/v1/services/{slug}/ratings", json={
                "score": 5, "comment": "Legit review",
            }, headers={"Authorization": auth_header})
            assert resp1.status_code == 201

            resp2 = await payment_client.post(f"/api/v1/services/{slug}/ratings", json={
                "score": 1, "comment": "Replay review",
            }, headers={"Authorization": auth_header})
            assert resp2.status_code == 402

    @pytest.mark.asyncio
    async def test_different_tokens_both_accepted(self, payment_client):
        """Two different L402 tokens (separate payments) should both work."""
        with patch.object(settings, "AUTH_ROOT_KEY", "replay-test-key"), \
             patch("app.payment.create_invoice", new_callable=AsyncMock) as mock_inv, \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_l402_inv, \
             patch("app.l402.check_payment_status", new_callable=AsyncMock) as mock_status:
            mock_inv.return_value = MOCK_INVOICE
            mock_l402_inv.return_value = MOCK_INVOICE
            # Legit payment: invoice settled for at least the endpoint price.
            # Lets these tests isolate replay logic from the amount check.
            mock_status.return_value = (True, 1_000_000)

            auth1, _ = _make_l402_token(b"first-payment-preimage")
            auth2, _ = _make_l402_token(b"second-payment-preimage")

            resp1 = await payment_client.post("/api/v1/services", json={
                "name": "First Paid Service",
                "url": "https://first-paid.example.com",
                "category_ids": [1],
            }, headers={"Authorization": auth1})
            assert resp1.status_code == 201

            resp2 = await payment_client.post("/api/v1/services", json={
                "name": "Second Paid Service",
                "url": "https://second-paid.example.com",
                "category_ids": [1],
            }, headers={"Authorization": auth2})
            assert resp2.status_code == 201

    @pytest.mark.asyncio
    async def test_replay_returns_fresh_challenge(self, payment_client):
        """Blocked replay should include a fresh WWW-Authenticate header."""
        with patch.object(settings, "AUTH_ROOT_KEY", "replay-test-key"), \
             patch("app.payment.create_invoice", new_callable=AsyncMock) as mock_inv, \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_l402_inv, \
             patch("app.l402.check_payment_status", new_callable=AsyncMock) as mock_status:
            mock_inv.return_value = MOCK_INVOICE
            mock_l402_inv.return_value = MOCK_INVOICE
            # Legit payment: invoice settled for at least the endpoint price.
            # Lets these tests isolate replay logic from the amount check.
            mock_status.return_value = (True, 1_000_000)

            auth_header, _ = _make_l402_token(b"challenge-replay-preimage")

            resp1 = await payment_client.post("/api/v1/services", json={
                "name": "Challenge Service",
                "url": "https://challenge-service.example.com",
                "category_ids": [1],
            }, headers={"Authorization": auth_header})
            assert resp1.status_code == 201

            resp2 = await payment_client.post("/api/v1/services", json={
                "name": "Replay Service",
                "url": "https://replay-service.example.com",
                "category_ids": [1],
            }, headers={"Authorization": auth_header})
            assert resp2.status_code == 402
            assert "www-authenticate" in resp2.headers
            www_auth = resp2.headers["www-authenticate"]
            assert www_auth.startswith("L402 ")
            assert "macaroon=" in www_auth
            assert "invoice=" in www_auth

    @pytest.mark.asyncio
    async def test_cross_endpoint_replay_blocked(self, payment_client):
        """Token used for submission cannot be reused for a rating."""
        # Create service in test mode
        resp = await payment_client.post("/api/v1/services", json={
            "name": "Cross Endpoint Target",
            "url": "https://cross-endpoint-target.example.com",
            "category_ids": [1],
        })
        assert resp.status_code == 201
        slug = resp.json()["slug"]

        with patch.object(settings, "AUTH_ROOT_KEY", "replay-test-key"), \
             patch("app.payment.create_invoice", new_callable=AsyncMock) as mock_inv, \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_l402_inv, \
             patch("app.l402.check_payment_status", new_callable=AsyncMock) as mock_status:
            mock_inv.return_value = MOCK_INVOICE
            mock_l402_inv.return_value = MOCK_INVOICE
            # Legit payment: invoice settled for at least the endpoint price.
            # Lets these tests isolate replay logic from the amount check.
            mock_status.return_value = (True, 1_000_000)

            auth_header, _ = _make_l402_token(b"cross-endpoint-preimage")

            # Use token to submit a service
            resp1 = await payment_client.post("/api/v1/services", json={
                "name": "Cross Endpoint Service",
                "url": "https://cross-endpoint-service.example.com",
                "category_ids": [1],
            }, headers={"Authorization": auth_header})
            assert resp1.status_code == 201

            # Try to reuse same token for a rating
            resp2 = await payment_client.post(f"/api/v1/services/{slug}/ratings", json={
                "score": 5, "comment": "Stolen token review",
            }, headers={"Authorization": auth_header})
            assert resp2.status_code == 402


# ---------------------------------------------------------------------------
# Lightning outage must not block x402 (issue #19)
# ---------------------------------------------------------------------------

class TestLightningOutageFallback:
    SUBMIT_BODY = {
        "name": "Outage Fallback Service",
        "url": "https://outage-fallback.example.com",
        "category_ids": [1],
    }

    @pytest.mark.asyncio
    async def test_x402_only_challenge_when_lightning_down(self, payment_client):
        from fastapi import HTTPException
        down = HTTPException(status_code=502, detail="Payment service unavailable")
        with patch.object(settings, "AUTH_ROOT_KEY", "outage-test-key"), \
             patch.object(settings, "X402_PAY_TO", "0x" + "ab" * 20), \
             patch("app.payment.create_invoice", new_callable=AsyncMock, side_effect=down):
            resp = await payment_client.post("/api/v1/services", json=self.SUBMIT_BODY)
        assert resp.status_code == 402
        assert "payment-required" in resp.headers
        assert "www-authenticate" not in resp.headers
        detail = resp.json()["detail"]
        assert detail["lightning_unavailable"] is True
        assert set(detail["how_to_pay"]) == {"x402"}

    @pytest.mark.asyncio
    async def test_502_when_lightning_down_and_no_x402(self, payment_client):
        from fastapi import HTTPException
        down = HTTPException(status_code=502, detail="Payment service unavailable")
        with patch.object(settings, "AUTH_ROOT_KEY", "outage-test-key"), \
             patch.object(settings, "X402_PAY_TO", ""), \
             patch("app.payment.create_invoice", new_callable=AsyncMock, side_effect=down):
            resp = await payment_client.post("/api/v1/services", json=self.SUBMIT_BODY)
        assert resp.status_code == 502
