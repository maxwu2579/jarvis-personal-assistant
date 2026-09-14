"""Only fixed safe messages may cross the calendar integration boundary."""

ERRORS = {
    "CALENDAR_NOT_CONFIGURED": (503, "Microsoft Calendar client ID is not configured"),
    "CALENDAR_CONFIG_INVALID": (503, "Microsoft Calendar configuration is invalid"),
    "CALENDAR_NOT_CONNECTED": (401, "Connect Microsoft Outlook to read your calendar"),
    "CALENDAR_AUTH_FAILED": (401, "Microsoft authorization failed; please connect again"),
    "CALENDAR_AUTH_STATE_INVALID": (400, "Authorization expired or is invalid; please connect again"),
    "CALENDAR_AUTH_STORAGE_UNAVAILABLE": (503, "Encrypted calendar credential storage is unavailable"),
    "CALENDAR_PERMISSION_DENIED": (403, "Calendar read permission was denied"),
    "CALENDAR_WRITE_AUTH_REQUIRED": (403, "Authorize Outlook calendar write access before creating an event"),
    "CALENDAR_WRITE_PERMISSION_DENIED": (403, "Calendar write permission was denied"),
    "CALENDAR_CREATE_INVALID": (422, "Calendar event data is invalid"),
    "CALENDAR_EVENT_TIME_PASSED": (422, "Calendar event start time has passed; create a new proposal"),
    "CALENDAR_CREATE_FAILED": (502, "Microsoft Calendar event could not be created safely"),
    "CALENDAR_RATE_LIMITED": (429, "Microsoft Calendar is rate limited; please try later"),
    "CALENDAR_UPSTREAM_ERROR": (502, "Microsoft Calendar could not be read completely"),
    "CALENDAR_INVALID_RANGE": (422, "Use timezone-aware start and end within a maximum of 31 days"),
    "CALENDAR_LOCAL_ONLY": (403, "Calendar access requires the trusted local application"),
}


class CalendarError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.status, self.message = ERRORS[code]
        self.retryable = retryable
        super().__init__(self.message)
