"""Masks personal data before any model call. The original record stays in TinyDB."""
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
BOOKING_ID = re.compile(r"\bsw[-_]?\d{6}\b", re.IGNORECASE)

MASK_BY_FIELD = {"guest_name": "[GUEST]", "guest_email": "[EMAIL]", "booking_id": "[BOOKING_ID]"}
# Only free text is scanned for patterns; a phone pattern would also match dates like 2026-10-13.
FREE_TEXT_FIELDS = {"guest_message", "host_note"}


def redact(record: dict) -> tuple[dict, int]:
    """Returns (redacted copy of a canonical-key record, number of values masked)."""
    masked, out = 0, {}
    for key, value in record.items():
        if key in MASK_BY_FIELD and value not in (None, ""):
            out[key] = MASK_BY_FIELD[key]
            masked += 1
        elif key in FREE_TEXT_FIELDS and isinstance(value, str):
            new = EMAIL.sub("[EMAIL]", value)
            new = PHONE.sub("[PHONE]", new)
            new = BOOKING_ID.sub("[BOOKING_ID]", new)
            masked += new != value
            out[key] = new
        else:
            out[key] = value
    return out, masked
