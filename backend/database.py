# database.py — Async PostgreSQL via SQLAlchemy
# Auto-converts postgres:// or postgresql:// to the postgresql+asyncpg:// format
# required by SQLAlchemy's asyncpg driver.

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings


def _to_asyncpg_url(url: str) -> str:
    """
    SQLAlchemy asyncpg driver requires postgresql+asyncpg://
    This converts the shorter postgres:// or postgresql:// forms automatically.
    """
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


_db_url = _to_asyncpg_url(settings.DATABASE_URL)

engine = create_async_engine(
    _db_url,
    echo=settings.DEBUG,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=300,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a database session.
    
    FIX: Removed auto-commit from the dependency. Each route manages its own
    commit/rollback. The previous auto-commit caused a double-commit error
    (asyncpg InFailedSQLTransactionError → HTTP 400) whenever a route
    called db.commit() internally (e.g. password_login when saving
    approval_status changes).
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
