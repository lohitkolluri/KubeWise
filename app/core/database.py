import pathlib
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Generator

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt, 
    wait_exponential,
    before_log,
    after_log
)

from app.core.exceptions import DatabaseError

# Create data directory if it doesn't exist
data_dir = pathlib.Path("data")
data_dir.mkdir(parents=True, exist_ok=True)

# SQLite database URL
SQLALCHEMY_DATABASE_URL = "sqlite:///data/kubewise.db"
ASYNC_SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:///data/kubewise.db"

# Create the engine for synchronous operations with optimal settings
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,  # Check connection is active
    pool_recycle=300,    # Recycle connection every 5 minutes
    pool_size=5,         # Connection pool size
    max_overflow=10      # Allow up to 10 connections beyond pool_size
)

# Configure timeout for SQLite operations
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout = 5000")  # 5 second timeout
    cursor.execute("PRAGMA journal_mode = WAL")   # Use WAL mode for better concurrency
    cursor.close()

# Create the async engine with appropriate settings for SQLite
# Note: pool_size and max_overflow are not supported for SQLite with aiosqlite
async_engine = create_async_engine(
    ASYNC_SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
    pool_recycle=300
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
        """Create all tables in the database with proper error handling."""
        try:
            Base.metadata.create_all(bind=self.engine)
            logger.info("Created SQLite database tables")
        except SQLAlchemyError as e:
            error_msg = f"Could not create database tables: {e}"
            logger.error(error_msg)
            raise DatabaseError(error_msg, {"error": str(e)})
    
    @retry(
        retry=retry_if_exception_type(SQLAlchemyError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before=before_log(logger, "INFO"),
        after=after_log(logger, "WARNING")
    )
    def get_db(self) -> Session:
        """Get database session with retry logic."""
        try:
            db = SessionLocal()
            # Test connection is valid
            db.execute("SELECT 1")
            return db
        except SQLAlchemyError as e:
            logger.error(f"Database connection error: {e}")
            if db:
                db.close()
            raise DatabaseError(f"Failed to connect to database", {"error": str(e)})

    @asynccontextmanager
    async def get_async_db_context(self) -> AsyncGenerator[AsyncSession, None]:
        """Context manager for async DB sessions with improved error handling."""
        session = AsyncSessionLocal()
        try:
            yield session
        except SQLAlchemyError as e:
            await session.rollback()
            error_msg = f"Database error in async session: {e}"
            logger.error(error_msg)
            raise DatabaseError(error_msg, {"error": str(e)})
        finally:
            await session.close()


# Create a global database instance
database = Database()


def get_db() -> Session:
    """Get database session - sync version with error handling."""
    return database.get_db()


@asynccontextmanager
async def get_async_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for database sessions."""
    async with database.get_async_db_context() as session:
        yield session


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """Get database session - async version with yield pattern for FastAPI dependency injection."""
    async with database.get_async_db_context() as session:
        yield session
