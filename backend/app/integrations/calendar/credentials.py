"""Windows current-user DPAPI only. No plaintext fallback or DB storage."""
import os
import sys
from pathlib import Path

from msal_extensions import FilePersistenceWithDataProtection, PersistedTokenCache

from app.core.config import PROJECT_ROOT
from .errors import CalendarError


def cache_path(client_id: str) -> Path:
    try:
        root = Path(os.environ["LOCALAPPDATA"])
        if not root.is_absolute():
            raise ValueError()
        path = (root / "JARVIS" / "auth" / f"microsoft-{client_id}.bin").resolve()
        if path.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError()
        return path
    except (KeyError, ValueError, OSError):
        raise CalendarError("CALENDAR_AUTH_STORAGE_UNAVAILABLE") from None


def encrypted_cache(client_id: str) -> PersistedTokenCache:
    try:
        if sys.platform != "win32":
            raise ValueError()
        persistence = FilePersistenceWithDataProtection(str(cache_path(client_id)))
        if persistence.is_encrypted is not True:
            raise ValueError()
        cache = PersistedTokenCache(persistence)
        if cache.is_encrypted is not True:
            raise ValueError()
        return cache
    except Exception:
        raise CalendarError("CALENDAR_AUTH_STORAGE_UNAVAILABLE") from None
