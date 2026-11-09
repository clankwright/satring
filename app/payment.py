"""Unified multi-protocol payment gate: L402 (Lightning) + x402 (USDC) + MPP (Lightning).

Routes requests to the appropriate protocol handler based on headers.
When multiple protocols are configured, a 402 challenge includes all
applicable headers so clients can pick whichever they support.

Header routing:
  - Authorization: L402/LSAT ... -> L402 path
  - Authorization: Payment ...   -> MPP path (Lightning via Payment auth scheme)
  - PAYMENT-SIGNATURE header     -> x402 path (USDC via facilitator)
  - No auth                      -> 402 with all protocol challenges
"""

import logging

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi.responses import JSONResponse

from app.config import settings, payments_enabled, x402_enabled
from app.l402 import require_l402, create_invoice, mint_macaroon, check_and_consume_payment
from app.mpp import require_mpp, build_mpp_challenge, build_receipt
from app.x402 import require_x402, build_payment_required

logger = logging.getLogger("satring.payment")


def attach_payment_receipt(response: JSONResponse, settlement: dict | None) -> JSONResponse:
    """Attach Payment-Receipt and Cache-Control headers if this was an MPP payment."""
    if not settlement:
        return response
    if settlement.get("_protocol") == "mpp":
        payment_hash = settlement.get("payment_hash", "")
        if payment_hash:
            response.headers["Payment-Receipt"] = build_receipt(payment_hash)
            response.headers["Cache-Control"] = "private"
    return response


async def require_payment(
    request: Request,
    amount_sats: int,
    price_usd: str,
    memo: str,
    db: AsyncSession | None = None,
) -> dict | None:
    """Unified payment gate supporting L402, x402, and MPP protocols.

    Returns None (L402/MPP path or test mode) or a settlement dict (x402 path).
    Raises HTTPException(402) with appropriate challenge headers if unpaid.

    Pass `db` to enable replay protection via ConsumedPayment table.
    """
    # Test mode bypass
    if not payments_enabled():
        return None

    auth = request.headers.get("Authorization", "")
    has_l402 = auth.startswith("L402 ") or auth.startswith("LSAT ")
    has_mpp = auth.startswith("Payment ")
    has_x402 = bool(request.headers.get("payment-signature"))

    # L402 auth header present: delegate to L402 handler
    if has_l402:
        await require_l402(request=request, db=db, amount_sats=amount_sats, memo=memo)
        return None

    # MPP Payment auth header present: delegate to MPP handler
    if has_mpp:
        mpp_result = await require_mpp(request=request, db=db, amount_sats=amount_sats, memo=memo)
        # Return receipt info so callers can attach Payment-Receipt header
        if mpp_result:
            mpp_result["_protocol"] = "mpp"
        return mpp_result

    # x402 payment signature present: delegate to x402 handler
    if has_x402 and x402_enabled():
        settlement = await require_x402(
            request=request,
            price_usd=price_usd,
            description=memo,
        )

        # SECURITY: Record tx hash to prevent replay of the same settlement.
        # The facilitator handles on-chain replay, but this prevents a client
        # from reusing a valid PAYMENT-SIGNATURE to hit our endpoint twice.
        tx_hash = settlement.get("transaction", "") if settlement else ""
        if not tx_hash:
            logger.error("x402 settlement succeeded but returned no transaction hash")
            raise HTTPException(status_code=502, detail="x402 settlement missing transaction hash")
        if db is not None:
            consumed = await check_and_consume_payment(tx_hash, db)
            if not consumed:
                logger.warning(f"x402 replay blocked: tx={tx_hash}")
                raise HTTPException(
                    status_code=402,
                    detail="x402 payment already consumed",
                )

        return settlement

    # No auth headers: return 402 challenge with all configured protocols.
    # A Lightning backend outage must not take down the x402 path: if the
    # invoice can't be minted and x402 is configured, issue an x402-only
    # challenge instead of a 502.
    try:
        invoice_data = await create_invoice(amount_sats, memo)
    except HTTPException:
        if not x402_enabled():
            raise
        logger.warning("Lightning invoice creation failed; issuing x402-only challenge")
        invoice_data = None

    headers = {"Cache-Control": "no-store"}
    base = settings.BASE_URL.rstrip("/")
    body = {
        "detail": "Payment Required",
        "price": {"sats": amount_sats, "usd": price_usd},
        "how_to_pay": {},
    }

    if invoice_data is not None:
        # L402 challenge
        macaroon_b64 = mint_macaroon(invoice_data["payment_hash"])
        l402_challenge = (
            f'L402 macaroon="{macaroon_b64}", '
            f'invoice="{invoice_data["payment_request"]}"'
        )
        # MPP challenge (uses same invoice, different wire format)
        mpp_challenge = build_mpp_challenge(
            amount_sats, invoice_data["payment_hash"],
            invoice_data["payment_request"], memo,
        )
        # Combine L402 and MPP in WWW-Authenticate (comma-separated per RFC 9110)
        headers["WWW-Authenticate"] = f"{l402_challenge}, {mpp_challenge}"
        body["how_to_pay"]["L402"] = {
            "steps": [
                "1. Extract macaroon and invoice from WWW-Authenticate header",
                "2. Pay the BOLT11 Lightning invoice",
                "3. Retry with header: Authorization: L402 <macaroon>:<preimage>",
            ],
            "docs": f"{base}/.well-known/l402",
        }
        body["how_to_pay"]["MPP"] = {
            "steps": [
                "1. Extract Payment challenge from WWW-Authenticate header",
                "2. Pay the BOLT11 invoice in the challenge request field",
                "3. Build credential with paymentHash and preimage",
                "4. Retry with header: Authorization: Payment <base64url-credential>",
            ],
            "docs": f"{base}/.well-known/mpp",
        }
    else:
        body["lightning_unavailable"] = True

    # Add x402 challenge if configured
    if x402_enabled():
        resource_url = str(request.url)
        x402_challenge = build_payment_required(price_usd, memo, resource_url, request.method)
        headers["PAYMENT-REQUIRED"] = x402_challenge

    if x402_enabled():
        body["how_to_pay"]["x402"] = {
            "steps": [
                "1. Decode base64 PAYMENT-REQUIRED header to get payment details",
                "2. Send USDC to the payTo address on Base (eip155:8453)",
                "3. Retry with header: PAYMENT-SIGNATURE <base64-signed-payload>",
            ],
            "docs": f"{base}/.well-known/x402",
        }

    raise HTTPException(
        status_code=402,
        detail=body,
        headers=headers,
    )
