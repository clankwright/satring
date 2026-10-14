import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

_db_url = settings.database_url

engine = create_async_engine(_db_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migrate: add columns to existing services table if missing.
    # information_schema is PostgreSQL; create_all already built a fresh schema elsewhere.
    async with engine.begin() as conn:
        if conn.dialect.name != "postgresql":
            return
        result = await conn.execute(sqlalchemy.text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'services'"
        ))
        existing_cols = {row[0] for row in result.fetchall()}

        migrations = [
            ("x402_network", "VARCHAR(50)"),
            ("x402_asset", "VARCHAR(100)"),
            ("x402_pay_to", "VARCHAR(100)"),
            ("pricing_usd", "VARCHAR(20)"),
            ("avg_latency_ms", "FLOAT"),
            ("total_checks", "INTEGER DEFAULT 0"),
            ("successful_checks", "INTEGER DEFAULT 0"),
            ("mpp_method", "VARCHAR(50)"),
            ("mpp_realm", "VARCHAR(200)"),
            ("mpp_currency", "VARCHAR(50)"),
            ("hit_count_total", "INTEGER DEFAULT 0"),
            ("hit_count_7d", "INTEGER DEFAULT 0"),
            ("hit_count_30d", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in migrations:
            if col_name not in existing_cols:
                await conn.execute(
                    sqlalchemy.text(f"ALTER TABLE services ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
                )

        # Rename status 'dead' -> 'down'
        await conn.execute(
            sqlalchemy.text("UPDATE services SET status = 'down' WHERE status = 'dead'")
        )
        # probe_history may not exist on fresh DB
        try:
            await conn.execute(
                sqlalchemy.text("UPDATE probe_history SET status = 'down' WHERE status = 'dead'")
            )
        except Exception:
            pass
