```
 ___  __ _| |_ _ __(_)_ __   __ _
/ __|/ _` | __| '__| | '_ \ / _` |
\__ \ (_| | |_| |  | | | | | (_| |
|___/\__,_|\__|_|  |_|_| |_|\__, |
                             |___/  L402 + x402 + MPP
```

<img src="app/static/img/satring-logo-trans-bg.png" alt="satring" width="20"> [satring.com](https://satring.com) — curated paid API directory for AI agents. Find, rate, and connect to paid APIs via Lightning (L402), USDC on Base (x402), or Stripe/Tempo (MPP).

Satring helps AI agents and developers discover paid API services that accept payments via the [L402 protocol](https://www.l402.org/) (Bitcoin Lightning), the [x402 protocol](https://www.x402.org/) (USDC on Base), or the [Machine Payments Protocol](https://mpp.dev/) (Stripe/Tempo). Browse the curated directory, submit your service, and let agents find you.

[![Watch the demo](https://img.youtube.com/vi/tjcg0qo5mMo/maxresdefault.jpg)](https://youtu.be/tjcg0qo5mMo)
**▶ Watch the 3-minute demo**

## Why

AI agents can now [pay for APIs autonomously](https://lightning.engineering/posts/2026-02-11-ln-agent-tools/) using Lightning, USDC, or Stripe. But there's no good way to discover what's available. Satring is the only curated, health-monitored directory with human ratings across all three payment protocols.

## Features

- **Stats landing page** with live directory metrics: protocol breakdown, category coverage, health status, growth
- Browse, search, and filter paid APIs by category, status, and protocol
- **Three-protocol support**: L402 (Bitcoin Lightning), x402 (USDC on Base), and MPP (Stripe/Tempo)
- Protocol checkboxes on submit/edit forms for multi-protocol services (e.g. L402+MPP)
- Submit services with payment gate (anti-spam), payable via L402, MPP, or x402
- Ratings and reputation system (also payment-gated via any protocol)
- Edit your listing with secure edit tokens
- Recover lost edit tokens via domain verification (`.well-known/satring-verify`)
- Shared edit tokens across same-domain services; one token manages all your listings
- JSON API for programmatic access and agent queries
- **Daily free API quota** (10 results/IP/day) with unlimited access via paid bulk endpoint
- Premium endpoints (bulk export, analytics, reputation) gated via L402, MPP, or x402
- Per-service health analytics: uptime percentage, average latency, probe history
- Health probing with automatic protocol detection (L402, x402, MPP, or any combination)
- Service status tracking (live / confirmed / down / unverified)
- Anti-scraping protection with IP-based daily quotas and permanent bans

## Quick Start

```bash
git clone https://github.com/clankwright/satring.git
cd satring
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # configure wallet keys, or leave defaults for test mode
uvicorn app.main:app --reload
```

Open http://localhost:8000

In test mode (`AUTH_ROOT_KEY=test-mode`), all payment gates are bypassed.

## API

Full interactive docs at [satring.com/docs](https://satring.com/docs).

### Free endpoints

Free endpoints return up to 10 results per IP per day. For unlimited access, use the payment-gated `/services/bulk` endpoint.

```bash
# List services (paginated, filterable by category, status, and protocol)
curl "https://satring.com/api/v1/services?category=search&status=live&protocol=L402&page=1&page_size=20"

# Search (also filterable by status and protocol)
curl "https://satring.com/api/v1/search?q=satring&protocol=x402"

# Service details
curl https://satring.com/api/v1/services/my-service

# List ratings
curl https://satring.com/api/v1/services/my-service/ratings

# List categories (use IDs in category_ids when submitting)
curl https://satring.com/api/v1/categories
```

### Payment-gated endpoints

These require payment via **L402**, **MPP** (Lightning), or **x402** (USDC). Without auth headers, the server returns `402` with challenges for all configured protocols.

Each service requires 1 to 2 `category_ids`: 1=ai/ml, 2=data, 3=finance, 4=identity, 5=media, 6=social, 7=search, 8=storage, 9=tools.

#### Option A: L402 (Lightning)

```bash
# Submit a service via L402
curl -X POST https://satring.com/api/v1/services \
  -H "Authorization: L402 <macaroon>:<preimage>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My API",
    "url": "https://api.example.com",
    "pricing_sats": 10,
    "pricing_model": "per-request",
    "protocol": "L402",
    "category_ids": [1, 2]
  }'
```

#### Option B: MPP (Lightning via Payment auth scheme)

```bash
# Submit a service via MPP
curl -X POST https://satring.com/api/v1/services \
  -H "Authorization: Payment <base64url-encoded-credential>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My API",
    "url": "https://api.example.com",
    "pricing_sats": 10,
    "pricing_model": "per-request",
    "protocol": "L402",
    "category_ids": [1, 2]
  }'
```

The MPP flow: hit the endpoint without auth to get a `402` with `WWW-Authenticate: Payment` containing a BOLT11 invoice, pay via Lightning, then retry with `Authorization: Payment <credential>` containing the preimage. See the [MPP spec](https://paymentauth.org/) for full details.

#### Option C: x402 (USDC on Base)

```bash
# Submit a service via x402
curl -X POST https://satring.com/api/v1/services \
  -H "PAYMENT-SIGNATURE: <base64-encoded-payment-json>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My API",
    "url": "https://api.example.com",
    "pricing_usd": "0.50",
    "pricing_model": "per-request",
    "protocol": "x402",
    "x402_network": "eip155:8453",
    "x402_asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "x402_pay_to": "0xYourWalletAddress",
    "category_ids": [1, 2]
  }'
```

#### Multi-protocol listings

A single service can support multiple payment rails. Use `+` to combine protocols (e.g. `L402+x402`, `L402+MPP`, `x402+MPP`, `L402+x402+MPP`) and include the required fields for each:

```bash
# L402 + MPP multi-protocol service
curl -X POST https://satring.com/api/v1/services \
  -H "Authorization: L402 <macaroon>:<preimage>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Dual API",
    "url": "https://api.example.com",
    "pricing_sats": 10,
    "pricing_model": "per-request",
    "protocol": "L402+MPP",
    "mpp_method": "tempo",
    "mpp_currency": "usd",
    "category_ids": [1, 2]
  }'
```

Multi-protocol services appear in search results when filtering by any of their constituent protocols.

#### MPP-only listing

```bash
# Pay with any supported protocol (L402, MPP, or x402)
curl -X POST https://satring.com/api/v1/services \
  -H "Authorization: Payment <base64url-credential>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My MPP API",
    "url": "https://mpp.api.example.com",
    "protocol": "MPP",
    "mpp_method": "tempo",
    "mpp_realm": "api.example.com",
    "mpp_currency": "usd",
    "category_ids": [1]
  }'
```

MPP fields: `mpp_method` (required: "tempo", "stripe", "lightning", "custom"), `mpp_realm` (optional), `mpp_currency` (optional).

#### Other gated operations

```bash
# Link to existing service on same domain (L402, MPP, or x402 auth)
curl -X POST https://satring.com/api/v1/services \
  -H "Authorization: L402 <macaroon>:<preimage>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Other API",
    "url": "https://api.example.com/v2",
    "existing_edit_token": "your-token-from-first-service"
  }'

# Edit a service
curl -X PATCH https://satring.com/api/v1/services/my-api \
  -H "X-Edit-Token: your-edit-token" \
  -H "Content-Type: application/json" \
  -d '{"description": "Updated description"}'

# Bulk export (use -i to see payment challenges in headers)
curl -i https://satring.com/api/v1/services/bulk

# Directory analytics (totals, health overview, pricing stats by protocol, growth, leaderboards, route usage)
curl -i https://satring.com/api/v1/analytics

# Per-service health analytics (uptime %, avg latency, probe history)
curl -i https://satring.com/api/v1/services/my-service/analytics

# Service reputation (rating distribution, monthly trends, peer comparison, review activity)
curl -i https://satring.com/api/v1/services/my-service/reputation
```

### Token recovery

If you lose your edit token, or want to edit a pre-seeded endpoint, prove domain ownership to get a new one:

```bash
# 1. Generate a challenge
curl -X POST https://satring.com/api/v1/services/my-service/recover/generate

# 2. Place the challenge code at your-domain.com/.well-known/satring-verify

# 3. Verify — new token is applied to ALL services on the same domain
curl -X POST https://satring.com/api/v1/services/my-service/recover/verify
```

### Rate limiting

Free API endpoints are rate limited at 6 requests/minute (burst of 5) with a daily quota of 10 results per IP. Payment-gated endpoints have a higher limit of 60 requests/minute (burst of 10). Rate-limited responses include a `Retry-After` header indicating how many seconds to wait.

### Health probes

The directory probes listed services periodically to detect protocol support and liveness. Probes detect L402 (`WWW-Authenticate: L402/LSAT`), x402 (`PAYMENT-REQUIRED` header), and MPP (`WWW-Authenticate: Payment`) protocols automatically. Probes attempt HEAD first, then fall back to GET if HEAD returns 405. Many nginx/reverse proxy configs reject HEAD by default, so if your service shows as "confirmed" rather than "live", this is normal and expected.

## Configuration

Environment variables (see `.env`):

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./db/sr.db` | Database connection string |
| `PAYMENT_URL` | — | Wallet instance URL |
| `PAYMENT_KEY` | — | Wallet invoice/read key |
| `AUTH_ROOT_KEY` | `test-mode` | Set to wallet key for production; `test-mode` bypasses payments |
| `AUTH_SUBMIT_PRICE_SATS` | `1000` | Cost to submit a service |
| `AUTH_REVIEW_PRICE_SATS` | `10` | Cost to submit a review |
| `AUTH_BULK_PRICE_SATS` | `5000` | Cost for bulk export |
| `AUTH_ANALYTICS_PRICE_SATS` | `500` | Cost for directory analytics |
| `AUTH_SERVICE_ANALYTICS_PRICE_SATS` | `50` | Cost for per-service health analytics |
| `AUTH_REPUTATION_PRICE_SATS` | `100` | Cost for reputation lookup |
| `AUTH_PRICE_SATS` | `100` | Default price for premium endpoints |
| `X402_FACILITATOR_URL` | `https://facilitator.xpay.sh` | x402 facilitator endpoint |
| `X402_PAY_TO` | — | USDC wallet address (empty = x402 disabled) |
| `X402_NETWORK` | `eip155:8453` | Chain ID (default: Base mainnet) |
| `X402_ASSET` | `0x8335...2913` | USDC contract address on Base |
| `AUTH_SUBMIT_PRICE_USD` | `0.50` | x402 cost to submit a service |
| `AUTH_REVIEW_PRICE_USD` | `0.01` | x402 cost to submit a review |
| `AUTH_BULK_PRICE_USD` | `2.50` | x402 cost for bulk export |
| `AUTH_ANALYTICS_PRICE_USD` | `0.25` | x402 cost for directory analytics |
| `AUTH_SERVICE_ANALYTICS_PRICE_USD` | `0.025` | x402 cost for per-service health analytics |
| `AUTH_REPUTATION_PRICE_USD` | `0.05` | x402 cost for reputation lookup |

## Scraper

The scraper discovers and indexes services from multiple sources:

```bash
# Scrape all 8 sources, probe endpoints, write to CSV for review
python db/scrape_sources.py

# Dry run (scrape + probe, print results, don't write CSV)
python db/scrape_sources.py --dry-run -v

# Re-probe existing DB services and update status
python db/scrape_sources.py --recheck

# Ingest reviewed CSV into the database
python db/scrape_sources.py --add-scraped
```

Sources: Lightning Faucet catalog, awesome-L402, Lightning Faucet Registry, domain crawler, GitHub search, x402.org ecosystem, awesome-x402, mpp.dev registry.

## MCP Server

The `satring-mcp` package lets AI agents discover and compare services from the directory programmatically via the [Model Context Protocol](https://modelcontextprotocol.io/).

```bash
pipx install satring-mcp
# or
pip install satring-mcp
```

Then add to your Claude Desktop `claude_desktop_config.json` or Claude Code `.claude/settings.json`:

```json
{
  "mcpServers": {
    "satring": {
      "command": "satring-mcp"
    }
  }
}
```

Tools: `discover_services`, `list_services`, `get_service`, `get_ratings`, `list_categories`, `compare_services`, `find_best_service`. See [`mcp/README.md`](mcp/README.md) for full docs.

[![PyPI](https://img.shields.io/pypi/v/satring-mcp)](https://pypi.org/project/satring-mcp/)

## Tech Stack

- **FastAPI** + **Jinja2** + **HTMX**: server-rendered with progressive enhancement
- **SQLAlchemy** (async) + **SQLite**: simple, no external DB needed
- **L402 / Macaroons**: Lightning-native authentication via [pymacaroons](https://github.com/ecordell/pymacaroons)
- **x402 / USDC on Base**: stablecoin payments via [xpay.sh](https://xpay.sh) facilitator
- **MPP**: Machine Payments Protocol Lightning payments via the [Payment HTTP auth scheme](https://paymentauth.org/)
- **Tailwind CSS** (browser CDN): terminal-themed green-on-black UI

## Contributing

All contributions welcome!

## License

[MIT](LICENSE)
