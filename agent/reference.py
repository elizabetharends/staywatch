"""The host's master data: canonical listings, regions, channels and field names.

This is what a property manager would keep in their own system. The agent cleans
toward these values; it does not import anything from the generator.
"""

# Canonical listing name -> (listing code, region)
LISTINGS = {
    "Seagrape Studio 1": ("SEAGRAPE-01", "Aruba"),
    "Seagrape Studio 2": ("SEAGRAPE-02", "Aruba"),
    "Coral Loft": ("CORAL-LOFT", "Aruba"),
    "Palm Court Villa": ("PALMCOURT", "Curaçao"),
    "Flamingo Flat": ("FLAMINGO", "Curaçao"),
    "Trade Winds Cottage": ("TW-COTTAGE", "Curaçao"),
}

REGIONS = {"aruba": "Aruba", "aw": "Aruba", "curacao": "Curaçao", "cw": "Curaçao"}

CHANNELS = {
    "airbnb": "airbnb", "abnb": "airbnb",
    "vrbo": "vrbo", "vrbocom": "vrbo",
    "direct": "direct", "directbooking": "direct", "website": "direct",
}

CATEGORIES = ["arrival_departure", "family_amenities", "pets", "local_tips", "logistics", "general"]
URGENCY_LEVELS = ["Low", "Moderate", "High", "Critical"]

# Every key name seen in exports -> the canonical field name
FIELD_ALIASES = {
    "booking_id": "booking_id", "bookingId": "booking_id",
    "listing": "listing", "listingName": "listing",
    "region": "region", "Region": "region",
    "channel": "channel", "source": "channel",
    "guest_name": "guest_name", "guest": "guest_name",
    "guest_email": "guest_email", "email": "guest_email",
    "booked_on": "booked_on", "bookedOn": "booked_on",
    "check_in": "check_in", "checkIn": "check_in",
    "nights": "nights", "nightsBooked": "nights",
    "booking_value": "booking_value", "totalPrice": "booking_value",
    "average_daily_rate": "average_daily_rate", "ADR": "average_daily_rate",
    "booking_window": "booking_window", "leadTime": "booking_window",
    "guest_message": "guest_message", "message": "guest_message",
    "host_note": "host_note", "notes": "host_note",
}

BLANK_VALUES = {"", "n/a", "na", "null", "none", "-"}


def canonical_keys(record: dict) -> dict:
    """Renames known keys to canonical names; unknown keys are kept as they are."""
    return {FIELD_ALIASES.get(k, k): v for k, v in record.items()}


def is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in BLANK_VALUES)
