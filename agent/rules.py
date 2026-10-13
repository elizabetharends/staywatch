"""The rules expert: deterministic cleaning and keyword classification.

Used when the kill switch is on, the token budget is spent, or a model is unavailable.
It never calls a model, so it is free, fast and predictable, but less flexible.
"""
import difflib
import re
import unicodedata
from datetime import date, datetime

from agent.reference import CATEGORIES, CHANNELS, LISTINGS, REGIONS, is_blank

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%Y/%m/%d", "%d/%m/%Y", "%b %d %Y"]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


_LISTING_KEYS = {}
for _name, (_code, _region) in LISTINGS.items():
    _LISTING_KEYS[_norm(_name)] = _name
    _LISTING_KEYS[_norm(_code)] = _name


def clean_listing(value) -> str | None:
    if is_blank(value):
        return None
    key = _norm(value)
    if key in _LISTING_KEYS:
        return _LISTING_KEYS[key]
    match = difflib.get_close_matches(key, list(_LISTING_KEYS), n=1, cutoff=0.75)
    return _LISTING_KEYS[match[0]] if match else None


def clean_region(value) -> str | None:
    return None if is_blank(value) else REGIONS.get(_norm(value))


def clean_channel(value) -> str | None:
    return None if is_blank(value) else CHANNELS.get(_norm(value))


def clean_date(value) -> date | None:
    if is_blank(value):
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def clean_money(value) -> float | None:
    """'$1,234.50', '1234.50 USD', '1 234.50' and 1234.5 all become 1234.5."""
    if is_blank(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"(?i)usd|\$", "", str(value)).strip()
    text = text.replace(",", "").replace(" ", "")
    try:
        return float(text)
    except ValueError:
        return None


def clean_int(value) -> int | None:
    if is_blank(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def clean_window_days(value) -> int | None:
    """35, '35 days' and '5.0 weeks' all become 35."""
    if is_blank(value):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(r"\s*([\d.]+)\s*(days?|weeks?)?\s*", str(value).lower())
    if not match:
        return None
    number = float(match.group(1))
    return round(number * 7) if (match.group(2) or "").startswith("week") else round(number)


def arrival_time(message) -> str | None:
    if is_blank(message):
        return None
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b", str(message).lower())
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3) == "pm" else 0)
    return f"{hour:02d}:{match.group(2) or '00'}"


def extract(record: dict) -> dict:
    """Cleans a canonical-key record into the Extraction fields."""
    return {
        "listing": clean_listing(record.get("listing")),
        "region": clean_region(record.get("region")),
        "channel": clean_channel(record.get("channel")),
        "booked_on": clean_date(record.get("booked_on")),
        "check_in": clean_date(record.get("check_in")),
        "nights": clean_int(record.get("nights")),
        "booking_value_usd": clean_money(record.get("booking_value")),
        "average_daily_rate_usd": clean_money(record.get("average_daily_rate")),
        "booking_window_days": clean_window_days(record.get("booking_window")),
        "arrival_time": arrival_time(record.get("guest_message")),
        "summary": None,  # rules do not write summaries
        "confidence": "high",
    }


KEYWORDS = {
    "arrival_departure": ["check-in", "checkin", "check in", "checkout", "check out", "land",
                          "flight", "arrive", "arrival", "late", "early"],
    "family_amenities": ["crib", "kids", "child", "towel", "bed", "baby"],
    "pets": ["dog", "cat", "pet"],
    "local_tips": ["restaurant", "tips", "anniversary", "celebrat", "excited", "things to do"],
    "logistics": ["parking", "wifi", "car", "pickup", "key", "directions"],
}
INJECTION_PATTERNS = re.compile(
    r"ignore (all |any )?(previous|prior) instructions|^system:|admin mode|"
    r"\bassistant\b.*\b(set|approve|delete|mark)\b|approve this import|delete the other",
    re.IGNORECASE,
)


def classify(record: dict) -> dict:
    text = " ".join(str(record.get(k) or "") for k in ("guest_message", "host_note")).lower()
    scores = {c: sum(w in text for w in words) for c, words in KEYWORDS.items()}
    best = max(scores, key=scores.get)
    category = best if scores[best] > 0 else "general"
    urgency = "Moderate" if category in {"arrival_departure", "logistics"} else "Low"
    return {"category": category, "urgency": urgency,
            "injection": bool(INJECTION_PATTERNS.search(text)),
            "confidence": 1.0 if scores[best] > 0 or not text.strip() else 0.5}


assert set(KEYWORDS) | {"general"} == set(CATEGORIES)
