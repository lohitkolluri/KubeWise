from typing import Optional, Dict, Any
from supabase import create_client, Client
from loguru import logger
from kubewise.config import settings

class SupabaseClient:
    _instance: Optional['SupabaseClient'] = None
    _client: Optional[Client] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._client:
            self._client = create_client(
                settings.supabase_url,
                settings.supabase_key
            )
            logger.info("Supabase client initialized")
    
    @property
    def client(self) -> Client:
        if not self._client:
            raise RuntimeError("Supabase client not initialized")
        return self._client
    
    async def insert_anomaly(self, anomaly_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self.client.table('anomalies').insert(anomaly_data).execute()
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error inserting anomaly: {e}")
            raise
    
    async def get_anomalies(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        try:
            result = self.client.table('anomalies')\
                .select('*')\
                .order('created_at', desc=True)\
                .limit(limit)\
                .offset(offset)\
                .execute()
            return result.data
        except Exception as e:
            logger.error(f"Error fetching anomalies: {e}")
            raise
    
    async def update_anomaly(self, anomaly_id: str, update_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self.client.table('anomalies')\
                .update(update_data)\
                .eq('id', anomaly_id)\
                .execute()
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error updating anomaly: {e}")
            raise
    
    async def get_metric_history(self, entity_id: str, metric_name: str, limit: int = 1000) -> List[Dict[str, Any]]:
        try:
            result = self.client.table('metric_history')\
                .select('*')\
                .eq('entity_id', entity_id)\
                .eq('metric_name', metric_name)\
                .order('timestamp', desc=True)\
                .limit(limit)\
                .execute()
            return result.data
        except Exception as e:
            logger.error(f"Error fetching metric history: {e}")
            raise