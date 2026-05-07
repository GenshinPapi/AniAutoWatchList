from __future__ import annotations

from datetime import datetime


def local_time(value: str | None, *, date_only: bool = False) -> str:
    if not value:
        return "-"
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")
    except ValueError:
        return value
