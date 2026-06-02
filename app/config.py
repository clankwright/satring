import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://satring@localhost/satring")
    DATABASE_PASSWORD: str = os.getenv("DATABASE_PASSWORD", "")

    @property
    def database_url(self) -> str:
        """Build DATABASE_URL with password injected if set separately."""
        url = self.DATABASE_URL
        pw = self.DATABASE_PASSWORD
        if pw and "://" in url:
            # Insert password into URL: scheme://user@host -> scheme://user:pw@host
            scheme_rest = url.split("://", 1)
            if "@" in scheme_rest[1] and ":" not in scheme_rest[1].split("@")[0]:
                user_host = scheme_rest[1].split("@", 1)
                return f"{scheme_rest[0]}://{user_host[0]}:{pw}@{user_host[1]}"
        return url
    PAYMENT_URL: str = os.getenv("PAYMENT_URL", "")
    PAYMENT_KEY: str = os.getenv("PAYMENT_KEY", "")
    AUTH_ROOT_KEY: str = os.getenv("AUTH_ROOT_KEY", "")
    AUTH_PRICE_SATS: int = int(os.getenv("AUTH_PRICE_SATS", "100"))
    AUTH_PRICE_USD: str = os.getenv("AUTH_PRICE_USD", "0.05")
    AUTH_SUBMIT_PRICE_SATS: int = int(os.getenv("AUTH_SUBMIT_PRICE_SATS", "1000"))
    AUTH_REVIEW_PRICE_SATS: int = int(os.getenv("AUTH_REVIEW_PRICE_SATS", "10"))
    AUTH_BULK_PRICE_SATS: int = int(os.getenv("AUTH_BULK_PRICE_SATS", "5000"))
    AUTH_ANALYTICS_PRICE_SATS: int = int(os.getenv("AUTH_ANALYTICS_PRICE_SATS", "500"))
    AUTH_REPUTATION_PRICE_SATS: int = int(os.getenv("AUTH_REPUTATION_PRICE_SATS", "100"))
    AUTH_SERVICE_ANALYTICS_PRICE_SATS: int = int(os.getenv("AUTH_SERVICE_ANALYTICS_PRICE_SATS", "50"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
    BASE_URL: str = os.getenv("BASE_URL", "https://satring.com")

    # x402 settings
    X402_FACILITATOR_URL: str = os.getenv("X402_FACILITATOR_URL", "https://facilitator.xpay.sh")
    X402_PAY_TO: str = os.getenv("X402_PAY_TO", "")           # USDC wallet address
    X402_NETWORK: str = os.getenv("X402_NETWORK", "eip155:8453")  # Base mainnet
    X402_ASSET: str = os.getenv("X402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")  # USDC on Base

    # USD prices (parallel to sat prices)
    AUTH_SUBMIT_PRICE_USD: str = os.getenv("AUTH_SUBMIT_PRICE_USD", "0.50")
    AUTH_REVIEW_PRICE_USD: str = os.getenv("AUTH_REVIEW_PRICE_USD", "0.01")
    AUTH_BULK_PRICE_USD: str = os.getenv("AUTH_BULK_PRICE_USD", "2.50")
    AUTH_ANALYTICS_PRICE_USD: str = os.getenv("AUTH_ANALYTICS_PRICE_USD", "0.25")
    AUTH_REPUTATION_PRICE_USD: str = os.getenv("AUTH_REPUTATION_PRICE_USD", "0.05")
    AUTH_SERVICE_ANALYTICS_PRICE_USD: str = os.getenv("AUTH_SERVICE_ANALYTICS_PRICE_USD", "0.025")
    AUTH_OWNER_AUDIENCE_PRICE_SATS: int = int(os.getenv("AUTH_OWNER_AUDIENCE_PRICE_SATS", "200"))
    AUTH_OWNER_AUDIENCE_PRICE_USD: str = os.getenv("AUTH_OWNER_AUDIENCE_PRICE_USD", "0.10")

    # Health probe settings
    HEALTH_PROBE_INTERVAL: int = int(os.getenv("HEALTH_PROBE_INTERVAL", "21600"))  # 6 hours
    HEALTH_PROBE_TIMEOUT: int = int(os.getenv("HEALTH_PROBE_TIMEOUT", "15"))       # seconds
    HEALTH_PROBE_CONCURRENCY: int = int(os.getenv("HEALTH_PROBE_CONCURRENCY", "10"))


settings = Settings()


def payments_enabled() -> bool:
    """SECURITY: Return True only if AUTH_ROOT_KEY is set to a real key.
    'test-mode' is the only value that explicitly disables payment gates."""
    return bool(settings.AUTH_ROOT_KEY) and settings.AUTH_ROOT_KEY != "test-mode"


def x402_enabled() -> bool:
    """Return True if x402 payments are configured (wallet address set)."""
    return bool(settings.X402_PAY_TO)

# SECURITY: Input length limits shared across API models, web form handlers,
# and HTML templates. Change values here — not in individual files.
MAX_NAME = 200
MAX_URL = 500
MAX_DESCRIPTION = 5000
MAX_OWNER_NAME = 200
MAX_OWNER_CONTACT = 300
MAX_LOGO_URL = 500
MAX_REVIEWER_NAME = 200
MAX_COMMENT = 2000
MAX_PRICING_SATS = 1_000_000
MAX_X402_NETWORK = 50
MAX_X402_ASSET = 100
MAX_X402_PAY_TO = 100
MAX_PRICING_USD = 20
MAX_MPP_METHOD = 50
MAX_MPP_REALM = 200
MAX_MPP_CURRENCY = 50

# SECURITY: Rate limits per IP. Change values here — not in individual files.
RATE_SUBMIT = "20/hour"
RATE_EDIT = "20/hour"
RATE_DELETE = "10/hour"
RATE_RECOVER = "20/hour"
RATE_REVIEW = "20/hour"
RATE_SEARCH = "2/second"
RATE_SEARCH_API = "6/minute"
RATE_LIST_API = "6/minute"
FREE_API_RESULTS_PER_DAY = 5        # max service summaries returned per IP per day (free tier)
RATE_DETAIL_API = "15/minute"
RATE_SITEMAP = "3/hour"
RATE_PAYMENT_STATUS = "30/minute"

# Endpoint usage tracking
USAGE_FLUSH_INTERVAL = 60       # seconds between DB flushes
USAGE_RETENTION_DAYS = 90       # auto-purge older data
