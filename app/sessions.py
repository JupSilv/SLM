from datetime import datetime, timezone
from zoneinfo import ZoneInfo


_SESSIONS = (
    {
        "id": "sydney",
        "name": "Sydney",
        "timezone": "Australia/Sydney",
        "opens": 8,
        "closes": 17,
        "currencies": frozenset({"AUD", "NZD"}),
    },
    {
        "id": "tokyo",
        "name": "Tóquio",
        "timezone": "Asia/Tokyo",
        "opens": 9,
        "closes": 18,
        "currencies": frozenset({"JPY", "AUD", "NZD"}),
    },
    {
        "id": "london",
        "name": "Londres",
        "timezone": "Europe/London",
        "opens": 8,
        "closes": 17,
        "currencies": frozenset({"EUR", "GBP", "CHF"}),
    },
    {
        "id": "new_york",
        "name": "Nova York",
        "timezone": "America/New_York",
        "opens": 8,
        "closes": 17,
        "currencies": frozenset({"USD", "CAD"}),
    },
)


def as_utc(moment):
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc)


def active_sessions(moment=None):
    moment_utc = as_utc(moment)
    active = []
    for session in _SESSIONS:
        local_time = moment_utc.astimezone(ZoneInfo(session["timezone"]))
        if (
            local_time.weekday() < 5
            and session["opens"] <= local_time.hour < session["closes"]
        ):
            active.append(session)
    return active


def preferred_currencies(sessions):
    return frozenset(
        currency
        for session in sessions
        for currency in session["currencies"]
    )


def session_names(sessions):
    return tuple(session["name"] for session in sessions)


def local_session_hours(moment):
    moment_utc = as_utc(moment)
    return {
        session["id"]: moment_utc.astimezone(
            ZoneInfo(session["timezone"])
        ).strftime("%Y-%m-%d %H:%M %Z")
        for session in _SESSIONS
    }


def session_ids_for(moment):
    return tuple(session["id"] for session in active_sessions(moment))
