from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.config import Config

class Base(DeclarativeBase):
    pass

def make_engine(cfg: Config):
    url = cfg.database_url
    # Railway often provides a sync URL (postgresql:// or postgres://).
    # For SQLAlchemy asyncio we must use the asyncpg driver.
    if url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+asyncpg://', 1)
    elif url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql+asyncpg://', 1)
    return create_async_engine(url, pool_pre_ping=True)

def make_session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
