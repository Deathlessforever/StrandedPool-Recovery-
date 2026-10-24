from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.flight_provider import _duration_minutes, _parse_datetime


def test_parse_datetime_accepts_utc_z_suffix():
    parsed = _parse_datetime("2026-10-21T09:15:00Z")

    assert parsed == datetime(2026, 10, 21, 9, 15, tzinfo=timezone.utc)


def test_parse_datetime_returns_none_for_invalid_values():
    assert _parse_datetime(None) is None
    assert _parse_datetime("not-a-date") is None
    assert _parse_datetime(12345) is None


def test_duration_parser_reads_provider_iso_duration():
    assert _duration_minutes({"duration": "PT2H15M"}, {}) == 135


def test_duration_parser_falls_back_to_segment_timestamps():
    option = {
        "segments": [
            {"departure_at": "2026-10-21T09:00:00Z"},
            {"arrival_at": "2026-10-21T11:30:00Z"},
        ]
    }

    assert _duration_minutes(option, {}) == 150


def test_duration_is_unknown_when_provider_has_no_duration_or_times():
    assert _duration_minutes({"id": "offer-without-timing"}, {}) is None
