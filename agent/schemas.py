"""Pydantic models that every expert's answer must pass, plus the JSON schemas sent to Claude."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from agent.reference import CATEGORIES, LISTINGS, URGENCY_LEVELS


class Classification(BaseModel):
    category: Literal[tuple(CATEGORIES)]
    urgency: Literal[tuple(URGENCY_LEVELS)]
    injection: bool
    confidence: float = Field(ge=0, le=1)          # confidence in the category


class Extraction(BaseModel):
    listing: str | None
    region: Literal["Aruba", "Curaçao"] | None
    channel: Literal["airbnb", "vrbo", "direct"] | None
    booked_on: date | None
    check_in: date | None
    nights: int | None = Field(ge=1, le=60)
    booking_value_usd: float | None = Field(gt=0, lt=100000)
    average_daily_rate_usd: float | None = Field(gt=0, lt=10000)
    booking_window_days: int | None = Field(ge=0, le=730)
    arrival_time: str | None = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    summary: str | None = Field(max_length=200)
    confidence: Literal["high", "low"]

    @field_validator("listing")
    @classmethod
    def known_listing(cls, value):
        if value is not None and value not in LISTINGS:
            raise ValueError(f"unknown listing {value!r}")
        return value


# Source fields each extracted value comes from. If the source was blank, the value
# must be null; anything else counts as an invented value.
SOURCE_OF = {
    "listing": "listing", "region": "region", "channel": "channel",
    "booked_on": "booked_on", "check_in": "check_in", "nights": "nights",
    "booking_value_usd": "booking_value", "average_daily_rate_usd": "average_daily_rate",
    "booking_window_days": "booking_window",
}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "listing": _nullable({"type": "string", "enum": list(LISTINGS)}),
        "region": _nullable({"type": "string", "enum": ["Aruba", "Curaçao"]}),
        "channel": _nullable({"type": "string", "enum": ["airbnb", "vrbo", "direct"]}),
        "booked_on": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
        "check_in": _nullable({"type": "string", "description": "YYYY-MM-DD"}),
        "nights": _nullable({"type": "integer"}),
        "booking_value_usd": _nullable({"type": "number"}),
        "average_daily_rate_usd": _nullable({"type": "number"}),
        "booking_window_days": _nullable({"type": "integer"}),
        "arrival_time": _nullable({"type": "string", "description": "HH:MM, 24-hour"}),
        "summary": _nullable({"type": "string"}),
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["listing", "region", "channel", "booked_on", "check_in", "nights",
                 "booking_value_usd", "average_daily_rate_usd", "booking_window_days",
                 "arrival_time", "summary", "confidence"],
    "additionalProperties": False,
}

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "urgency": {"type": "string", "enum": URGENCY_LEVELS},
        "injection": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["category", "urgency", "injection", "confidence"],
    "additionalProperties": False,
}
