from functools import lru_cache
from supabase import create_client, Client
from src.config import get_settings


@lru_cache()
def get_db() -> Client:
    settings = get_settings()
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
