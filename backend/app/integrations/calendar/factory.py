from functools import lru_cache
from app.core.config import settings
from .auth import MicrosoftAuth
from .microsoft import MicrosoftGraphCalendarProvider


@lru_cache(maxsize=1)
def get_calendar_auth():
    return MicrosoftAuth(settings)


def get_calendar_provider():
    return MicrosoftGraphCalendarProvider(get_calendar_auth())
