"""Unit tests for app/nwc.py and the NWC routing/fallback in app/l402.py."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.nwc as nwc
import app.l402 as l402


class _Resp:
    """Stand-in for a nostr-sdk response object (attribute access only)."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- app/nwc.py -------------------------------------------------------------

@pytest.mark.asyncio
async def test_make_invoice_converts_sats_to_msats_and_maps_fields():
    client = MagicMock()
    client.make_invoice = AsyncMock(return_value=_Resp(invoice="lnbc1xyz", payment_hash="abc"))
    captured = {}
    with patch("app.nwc._get_client", return_value=client), \
         patch("nostr_sdk.MakeInvoiceRequest", side_effect=lambda **kw: captured.update(kw)):
        out = await nwc.make_invoice(150, "listing fee")
    assert out == {"payment_hash": "abc", "payment_request": "lnbc1xyz"}
    assert captured["amount"] == 150_000          # sats -> msats
    assert captured["description"] == "listing fee"


@pytest.mark.asyncio
async def test_make_invoice_raises_on_missing_fields():
    client = MagicMock()
    client.make_invoice = AsyncMock(return_value=_Resp(invoice="", payment_hash=None))
    with patch("app.nwc._get_client", return_value=client), \
         patch("nostr_sdk.MakeInvoiceRequest", side_effect=lambda **kw: None):
        with pytest.raises(ValueError):
            await nwc.make_invoice(10, "m")


@pytest.mark.asyncio
async def test_lookup_invoice_paid_converts_msats_to_sats():
    client = MagicMock()
    client.lookup_invoice = AsyncMock(return_value=_Resp(settled_at=object(), amount=42_000))
    with patch("app.nwc._get_client", return_value=client), \
         patch("nostr_sdk.LookupInvoiceRequest", side_effect=lambda **kw: None):
        paid, sats = await nwc.lookup_invoice("hash")
    assert paid is True
    assert sats == 42


@pytest.mark.asyncio
async def test_lookup_invoice_unsettled_is_unpaid():
    client = MagicMock()
    client.lookup_invoice = AsyncMock(return_value=_Resp(settled_at=None, amount=0))
    with patch("app.nwc._get_client", return_value=client), \
         patch("nostr_sdk.LookupInvoiceRequest", side_effect=lambda **kw: None):
        paid, sats = await nwc.lookup_invoice("hash")
    assert paid is False
    assert sats == 0


# --- app/l402.py routing + fallback ----------------------------------------

def _fake_httpx_client(resp):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_create_invoice_uses_nwc_when_enabled():
    with patch("app.l402.nwc_enabled", return_value=True), \
         patch("app.l402.nwc_backend.make_invoice",
               AsyncMock(return_value={"payment_hash": "h", "payment_request": "r"})) as mk:
        out = await l402.create_invoice(100, "memo")
    assert out == {"payment_hash": "h", "payment_request": "r"}
    mk.assert_awaited_once_with(100, "memo")


@pytest.mark.asyncio
async def test_create_invoice_falls_back_to_lnbits_on_nwc_error():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"payment_hash": "lh", "payment_request": "lr"})
    with patch("app.l402.nwc_enabled", return_value=True), \
         patch("app.l402.nwc_backend.make_invoice", AsyncMock(side_effect=RuntimeError("relay down"))), \
         patch("app.l402.httpx.AsyncClient", return_value=_fake_httpx_client(resp)):
        out = await l402.create_invoice(100, "memo")
    assert out == {"payment_hash": "lh", "payment_request": "lr"}


@pytest.mark.asyncio
async def test_create_invoice_uses_lnbits_when_nwc_disabled():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"payment_hash": "lh", "payment_request": "lr"})
    with patch("app.l402.nwc_enabled", return_value=False), \
         patch("app.l402.nwc_backend.make_invoice", AsyncMock(side_effect=AssertionError("should not be called"))), \
         patch("app.l402.httpx.AsyncClient", return_value=_fake_httpx_client(resp)):
        out = await l402.create_invoice(100, "memo")
    assert out == {"payment_hash": "lh", "payment_request": "lr"}


@pytest.mark.asyncio
async def test_check_payment_status_uses_nwc_when_enabled():
    with patch("app.l402.nwc_enabled", return_value=True), \
         patch("app.l402.nwc_backend.lookup_invoice", AsyncMock(return_value=(True, 50))):
        paid, sats = await l402.check_payment_status("h")
    assert paid is True
    assert sats == 50


@pytest.mark.asyncio
async def test_check_payment_status_falls_back_to_lnbits_on_nwc_error():
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"paid": True, "details": {"amount": 70_000}})
    with patch("app.l402.nwc_enabled", return_value=True), \
         patch("app.l402.nwc_backend.lookup_invoice", AsyncMock(side_effect=RuntimeError("relay down"))), \
         patch("app.l402.httpx.AsyncClient", return_value=_fake_httpx_client(resp)):
        paid, sats = await l402.check_payment_status("h")
    assert paid is True
    assert sats == 70
