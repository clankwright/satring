from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Integer, String, Text, Float, DateTime, ForeignKey, Index, Table
)
from sqlalchemy.orm import relationship

from app.database import Base


service_categories = Table(
    "service_categories",
    Base.metadata,
    Column("service_id", Integer, ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", Integer, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
)


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, default="")

    services = relationship("Service", secondary=service_categories, back_populates="categories")


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(200), nullable=False, unique=True, index=True)
    url = Column(String(500), nullable=False)
    description = Column(Text, default="")
    pricing_sats = Column(Integer, default=0)
    pricing_model = Column(String(50), default="per-request")
    protocol = Column(String(20), default="L402")
    owner_name = Column(String(200), default="")
    owner_contact = Column(String(300), default="")
    logo_url = Column(String(500), default="")
    edit_token_hash = Column(String(64), nullable=True, index=True)
    domain_challenge = Column(String(64), nullable=True)
    domain_challenge_expires_at = Column(DateTime(timezone=True), nullable=True)
    domain_verified = Column(Boolean, default=False)
    x402_network = Column(String(50), nullable=True)     # "eip155:8453"
    x402_asset = Column(String(100), nullable=True)       # USDC contract address
    x402_pay_to = Column(String(100), nullable=True)      # wallet address
    pricing_usd = Column(String(20), nullable=True)       # "0.01"
    mpp_method = Column(String(50), nullable=True)         # "tempo", "stripe", "lightning"
    mpp_realm = Column(String(200), nullable=True)         # protection space / domain
    mpp_currency = Column(String(50), nullable=True)       # "usd" or token address
    avg_rating = Column(Float, default=0.0)
    rating_count = Column(Integer, default=0)
    status = Column(String(20), default="unverified")  # unverified | confirmed | live | down | purged
    last_probed_at = Column(DateTime(timezone=True), nullable=True)
    dead_since = Column(DateTime(timezone=True), nullable=True)
    avg_latency_ms = Column(Float, nullable=True)       # rolling 7-day average
    total_checks = Column(Integer, default=0)
    successful_checks = Column(Integer, default=0)
    hit_count_total = Column(Integer, default=0)    # lifetime views (denormalized from UsageDetail)
    hit_count_7d = Column(Integer, default=0)       # rolling 7-day views
    hit_count_30d = Column(Integer, default=0)      # rolling 30-day views
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    categories = relationship("Category", secondary=service_categories, back_populates="services")
    ratings = relationship("Rating", back_populates="service", cascade="all, delete-orphan")


class ProbeHistory(Base):
    __tablename__ = "probe_history"

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    probed_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False)        # live | confirmed | down
    response_time_ms = Column(Float, nullable=True)
    detected_protocol = Column(String(20), nullable=True)
    status_code = Column(Integer, nullable=True)
    error = Column(String(200), nullable=True)

    service = relationship("Service", backref="probe_history")


class ConsumedPayment(Base):
    __tablename__ = "consumed_payments"

    payment_hash = Column(String(128), primary_key=True)
    consumed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PendingSubmission(Base):
    """Form payload saved with the submission invoice.

    The listing is created when that invoice is paid, including when the
    client never sends the follow-up POST (it only polls payment status).
    """

    __tablename__ = "pending_submissions"

    payment_hash = Column(String(128), primary_key=True)
    payload = Column(Text, nullable=False)
    edit_token = Column(String(128), nullable=True)
    token_reused = Column(Boolean, default=False)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="SET NULL"), nullable=True)
    error = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    published_at = Column(DateTime(timezone=True), nullable=True)


class RouteUsage(Base):
    __tablename__ = "route_usage"

    id = Column(Integer, primary_key=True)
    route = Column(String(200), nullable=False, index=True)
    source = Column(String(10), nullable=False, index=True)  # "api" or "web"
    hour = Column(DateTime(timezone=True), nullable=False, index=True)
    hit_count = Column(Integer, default=0)
    unique_ips = Column(Integer, default=0)


class UsageDetail(Base):
    __tablename__ = "usage_detail"

    id = Column(Integer, primary_key=True)
    dimension = Column(String(20), nullable=False, index=True)  # "query", "category", "slug"
    value = Column(String(200), nullable=False)
    hour = Column(DateTime(timezone=True), nullable=False, index=True)
    hit_count = Column(Integer, default=0)
    unique_ips = Column(Integer, default=0)

    __table_args__ = (
        Index("ix_usage_detail_dim_val_hour", "dimension", "value", "hour"),
    )


class AgentUsage(Base):
    __tablename__ = "agent_usage"

    id = Column(Integer, primary_key=True)
    agent_class = Column(String(30), nullable=False, index=True)
    hour = Column(DateTime(timezone=True), nullable=False, index=True)
    hit_count = Column(Integer, default=0)
    unique_ips = Column(Integer, default=0)


class Rating(Base):
    __tablename__ = "ratings"

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    score = Column(Integer, nullable=False)
    comment = Column(Text, default="")
    reviewer_name = Column(String(200), default="Anonymous")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    service = relationship("Service", back_populates="ratings")


