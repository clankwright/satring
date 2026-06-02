"""Nostr Wallet Connect (NIP-47) Lightning backend.

When ``NWC_CONNECTION_URI`` is configured, Satring mints and looks up invoices
over NWC instead of the LNbits HTTP API. Only the invoice backend behind
``app.l402.create_invoice`` / ``check_payment_status`` swaps — the L402, x402
and MPP payment flows are unchanged.

Uses the rust-nostr ``nostr-sdk`` NWC client, which manages the relay
connection and NIP-44 encryption internally. NIP-47 amounts are in millisats.
"""
import logging

from app.config import settings

logger = logging.getLogger("satring.nwc")

_client = None  # lazily-created nostr_sdk.Nwc, reused across requests


def _get_client():
    """Return a process-wide NWC client, parsing the URI on first use."""
    global _client
    if _client is None:
        from nostr_sdk import Nwc, NostrWalletConnectUri
        uri = NostrWalletConnectUri.parse(settings.NWC_CONNECTION_URI)
        _client = Nwc(uri)
    return _client


async def make_invoice(amount_sats: int, memo: str) -> dict:
    """Mint a bolt11 invoice over NWC.

    Returns ``{"payment_hash": str, "payment_request": str}``. Raises on any NWC
    error (or a missing hash/invoice) so the caller can fall back to LNbits.
    """
    from nostr_sdk import MakeInvoiceRequest

    nwc = _get_client()
    resp = await nwc.make_invoice(MakeInvoiceRequest(
        amount=amount_sats * 1000,  # NIP-47 amounts are in millisats
        description=memo,
        description_hash=None,
        expiry=None,
    ))
    if not resp.payment_hash or not resp.invoice:
        raise ValueError("NWC make_invoice returned no payment_hash/invoice")
    return {"payment_hash": resp.payment_hash, "payment_request": resp.invoice}


async def lookup_invoice(payment_hash: str) -> tuple[bool, int]:
    """Return ``(paid, amount_sats)`` for a payment hash via NWC.

    ``paid`` is True only once the invoice has settled. Raises on any NWC error
    so the caller can fall back to LNbits.
    """
    from nostr_sdk import LookupInvoiceRequest

    nwc = _get_client()
    resp = await nwc.lookup_invoice(LookupInvoiceRequest(payment_hash=payment_hash, invoice=None))
    paid = resp.settled_at is not None
    amount_sats = (resp.amount or 0) // 1000
    return paid, amount_sats
