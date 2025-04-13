from motor.motor_asyncio import AsyncIOMotorClient
from loguru import logger
from app.core.config import settings

class Database:
    client: AsyncIOMotorClient = None
    db = None

    async def connect_to_database(self):
        """Create database connection."""
        try:
            self.client = AsyncIOMotorClient(settings.MONGODB_ATLAS_URI)
            self.db = self.client[settings.MONGODB_DB_NAME]
            logger.info("Connected to MongoDB Atlas")
        except Exception as e:
            logger.error(f"Could not connect to MongoDB: {e}")
            raise

    async def close_database_connection(self):
        """Close database connection."""
        if self.client:
            self.client.close()
            logger.info("Closed MongoDB connection")

# Create a global database instance
database = Database()

async def get_database():
    """Get database instance."""
    if database.db is None:
        await database.connect_to_database()
    return database.db
