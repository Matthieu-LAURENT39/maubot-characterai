from datetime import datetime


def pretty_utc_str(dt: datetime, /) -> str:
    """
    Converts a datetime with UTC timezone to a string, with some prettifying.
    """
    # We remove the milliseconds, because they make output too noisy
    # We replace the timezone with Z, to be more concise in showing it's UTC
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
