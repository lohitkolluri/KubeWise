import pathlib

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Create data directory if it doesn't exist
data_dir = pathlib.Path("data")
data_dir.mkdir(parents=True, exist_ok=True)

# SQLite database URL
SQLALCHEMY_DATABASE_URL = "sqlite:///data/kubewise.db"
ASYNC_SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:///data/kubewise.db"

# Create the engine for synchronous operations
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# Create the async engine
async_engine = create_async_engine(
    ASYNC_SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# SessionLocal is a factory that produces new database session objects
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# AsyncSessionLocal is a factory for async sessions
AsyncSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=async_engine, class_=AsyncSession
)

# Base class for SQLAlchemy models
Base = declarative_base()


class Database:
    def __init__(self):
        self.engine = engine
        self.async_engine = async_engine
        self.Base = Base

    def create_tables(self):
        """Create all tables in the database."""
        try:
            Base.metadata.create_all(bind=self.engine)
            logger.info("Created SQLite database tables")
        except Exception as e:
            logger.error(f"Could not create database tables: {e}")
            raise

    def get_db(self):
        """Get database session."""
        db = SessionLocal()
        try:
            return db
        finally:
            db.close()

    async def get_async_db(self):
        """Get async database session."""
        async with AsyncSessionLocal() as session:
            yield session


# Create a global database instance
database = Database()


def get_db():
    """Get database session - sync version."""
    db = SessionLocal()
    try:
        return db
    finally:
        db.close()


async def get_async_db():
    """Get database session - async version."""
    async with AsyncSessionLocal() as session:
        yield session
