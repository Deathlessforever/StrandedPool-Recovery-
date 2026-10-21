"""Production flight search adapter.

Only DRCT production responses are labelled LIVE. Sandbox responses are labelled
SANDBOX_TEST_DATA and must never be presented as real fares or availability.
The API key is read server-side from STRANDEDPOOL_DRCT_API_KEY.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/flights", tags=["live flights"])
DRCT_API_KEY_ENV = "STRANDEDPOOL_DRCT_API_KEY"
DRCT_SANDBOX = os.getenv("STRANDEDPOOL_DRCT_ENV", "production").strip().lower() == "sandbox"
DRCT_BASE_URL = (
    "https://sandbox-api.drct.aero" if DRCT_SANDBOX else "https://api.drct.aero"
)
DRCT_VERSION = os.getenv("STRANDEDPOOL_DRCT_VERSION", "2026-09-01")


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_minutes(option: dict, response: dict) -> int | None:
    """Use explicit provider duration first, otherwise derive from segment times."""
    for item in _walk_dicts(option):
        journey_seconds = item.get("journey_time")
        if isinstance(journey_seconds, (int, float)) and journey_seconds >= 0:
            return int(journey_seconds // 60)
        for key in ("duration_minutes", "total_duration_minutes", "duration_min"):
            if isinstance(item.get(key), (int, float)):
                return int(item[key])
        for key in ("duration", "total_duration"):
            raw = item.get(key)
            if isinstance(raw, (int, float)):
                # Provider duration numbers are not assumed to be minutes unless
                # the key explicitly says so.
                continue
            if isinstance(raw, str):
                import re
                hours = re.search(r"(\d+)H", raw.upper())
                minutes = re.search(r"(\d+)M", raw.upper())
                if hours or minutes:
                    return int(hours.group(1) if hours else 0) * 60 + int(minutes.group(1) if minutes else 0)

    times = []
    for item in _walk_dicts(option):
        for key in ("departure_at", "departure_time", "depart_at", "arrival_at", "arrival_time", "arrive_at"):
            parsed = _parse_datetime(item.get(key))
            if parsed:
                times.append((key, parsed))
    departures = [t for k, t in times if "depart" in k]
    arrivals = [t for k, t in times if "arriv" in k or "arrive" in k]
    if departures and arrivals:
        delta = (max(arrivals) - min(departures)).total_seconds()
        if delta >= 0:
            return int(delta // 60)
    return None


def _fare_value(option: dict, response: dict) -> tuple[float | None, str | None]:
    # Prefer explicit totals on the option. Then resolve fare references against
    # the top-level fares collection returned by DRCT.
    for item in _walk_dicts(option):
        for key in ("total_amount", "total_price", "price", "amount", "grand_total"):
            value = item.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                currency = item.get("currency") or item.get("currency_code")
                return float(value), str(currency) if currency else None
            if isinstance(value, dict):
                amount = value.get("amount") or value.get("value")
                if isinstance(amount, (int, float)) and amount >= 0:
                    currency = value.get("currency") or value.get("currency_code")
                    return float(amount), str(currency) if currency else None

    fare_ids = set()
    for item in _walk_dicts(option):
        for key in ("fare_id", "fare", "fares"):
            value = item.get(key)
            if isinstance(value, str):
                fare_ids.add(value)
            elif isinstance(value, list):
                fare_ids.update(v for v in value if isinstance(v, str))
            elif isinstance(value, dict) and isinstance(value.get("id"), str):
                fare_ids.add(value["id"])
    for fare in response.get("fares", []) if isinstance(response.get("fares"), list) else []:
        if not isinstance(fare, dict) or (fare_ids and fare.get("id") not in fare_ids):
            continue
        for key in ("total_amount", "total_price", "price", "amount", "grand_total"):
            value = fare.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                currency = fare.get("currency") or fare.get("currency_code")
                return float(value), str(currency) if currency else None
            if isinstance(value, dict):
                amount = value.get("amount") or value.get("value")
                if isinstance(amount, (int, float)) and amount >= 0:
                    currency = value.get("currency") or value.get("currency_code")
                    return float(amount), str(currency) if currency else None
    return None, None


def _transfer_minutes(option: dict, response: dict) -> int | None:
    """Derive connection layover time only when provider segment timestamps exist."""
    referenced = []
    for item in _walk_dicts(option):
        for key in ("segments", "segment_ids"):
            value = item.get(key)
            if isinstance(value, list):
                referenced.extend(value)
    provider_segments = response.get("segments", [])
    if not isinstance(provider_segments, list) or not provider_segments:
        return None
    if referenced and all(isinstance(value, str) for value in referenced):
        ordered = [segment for ref in referenced for segment in provider_segments
                   if isinstance(segment, dict) and segment.get("id") == ref]
    else:
        ordered = [segment for segment in referenced if isinstance(segment, dict)]
    intervals = []
    for segment in ordered:
        departures = []
        arrivals = []
        for item in _walk_dicts(segment):
            for key in ("departure_at", "departure_time", "depart_at", "at"):
                parsed = _parse_datetime(item.get(key))
                if parsed and key != "at":
                    departures.append(parsed)
            for key in ("arrival_at", "arrival_time", "arrive_at"):
                parsed = _parse_datetime(item.get(key))
                if parsed:
                    arrivals.append(parsed)
        intervals.append((min(departures) if departures else None, max(arrivals) if arrivals else None))
    if len(intervals) < 2 or any(not dep or not arr for dep, arr in intervals):
        return None
    layovers = []
    for previous, following in zip(intervals, intervals[1:]):
        delta = (following[0] - previous[1]).total_seconds()
        if delta < 0:
            return None
        layovers.append(int(delta // 60))
    return sum(layovers)


def _segment_count(option: dict, response: dict) -> int | None:
    segments = []
    for item in _walk_dicts(option):
        for key in ("segments", "segment_ids"):
            value = item.get(key)
            if isinstance(value, list):
                segments.extend(value)
    if segments:
        # Some APIs reference segments by id; use the provider's top-level
        # segments list to resolve IDs before counting.
        ids = {s.get("id") for s in response.get("segments", []) if isinstance(s, dict)}
        if ids and all(isinstance(s, str) for s in segments):
            count = sum(1 for s in response.get("segments", []) if isinstance(s, dict) and s.get("id") in segments)
            return count or None
        return len(segments)
    return None


def _normalise_options(payload: dict) -> list[dict]:
    raw_options = payload.get("flights_options")
    if not isinstance(raw_options, list):
        raise HTTPException(
            status_code=502,
            detail="Flight provider response did not contain a recognised flights_options list; no demo data was substituted.",
        )
    top_flights = payload.get("flights", [])
    top_segments = payload.get("segments", [])
    flights_by_id = {item.get("id"): item for item in top_flights
                     if isinstance(item, dict) and item.get("id")} if isinstance(top_flights, list) else {}
    segments_by_id = {item.get("id"): item for item in top_segments
                      if isinstance(item, dict) and item.get("id")} if isinstance(top_segments, list) else {}
    results = []

    for option_index, option in enumerate(raw_options):
        if not isinstance(option, dict):
            continue
        refs = option.get("flights", [])
        if isinstance(refs, list):
            linked_flights = [flights_by_id[ref] for ref in refs if isinstance(ref, str) and ref in flights_by_id]
            linked_flights += [ref for ref in refs if isinstance(ref, dict)]
        else:
            linked_flights = []
        offers = option.get("offers") if isinstance(option.get("offers"), list) else []
        if not offers:
            for flight in linked_flights:
                if isinstance(flight.get("offers"), list):
                    offers.extend(flight["offers"])
        # Some response versions put the offer fields directly on the option.
        if not offers:
            offers = [option]

        for offer_index, offer in enumerate(offers):
            if not isinstance(offer, dict):
                continue
            fare, currency = _fare_value(offer, payload)
            duration_values = []
            segment_refs = []
            for flight in linked_flights:
                seconds = flight.get("journey_time")
                if isinstance(seconds, (int, float)) and seconds >= 0:
                    duration_values.append(int(seconds // 60))
                segment_list = flight.get("segments", [])
                if isinstance(segment_list, list):
                    segment_refs.extend(segment_list)
            duration = sum(duration_values) if duration_values else _duration_minutes(offer, payload)
            if duration is None:
                duration = _duration_minutes(option, payload)
            if segment_refs:
                segment_count = sum(1 for ref in segment_refs if ref in segments_by_id) or len(segment_refs)
                resolved_segments = [segments_by_id[ref] for ref in segment_refs if ref in segments_by_id]
                transfer_seconds = [segment.get("transfer_time") for segment in resolved_segments if isinstance(segment.get("transfer_time"), (int, float)) and segment.get("transfer_time") >= 0]
                transfer_minutes = int(sum(transfer_seconds) // 60) if transfer_seconds else _transfer_minutes({"segments": segment_refs}, {"segments": list(segments_by_id.values())})
            else:
                segment_count = _segment_count(offer, payload)
                transfer_minutes = _transfer_minutes(offer, payload)
            stops = max(0, segment_count - 1) if segment_count is not None else None
            offer_id = offer.get("id") or offer.get("offer_id") or option.get("id")
            if not offer_id:
                offer_id = f"response-option-{option_index + 1}-offer-{offer_index + 1}"
            results.append({
                "provider": "DRCT",
                "provider_offer_id": offer_id,
                "fare": fare,
                "currency": currency,
                "duration_minutes": duration,
                "segments": segment_count,
                "stops": stops,
                "transfer_minutes": transfer_minutes,
                "availability_status": "provider_offer_returned_unrepriced",
                "verification": "PROVIDER_SEARCH_RESULT_UNPRICED" if not DRCT_SANDBOX else "SANDBOX_TEST_DATA",
                "simulated": DRCT_SANDBOX,
                "rank": None,
                "ranking_note": None,
                "raw_offer": offer,
            })

    results.sort(key=lambda item: (
        item["fare"] is None,
        item["duration_minutes"] is None,
        item["fare"] if item["fare"] is not None else float("inf"),
        item["duration_minutes"] if item["duration_minutes"] is not None else float("inf"),
        item["transfer_minutes"] if item["transfer_minutes"] is not None else float("inf"),
        item["stops"] if item["stops"] is not None else 10**6,
    ))
    for index, item in enumerate(results, start=1):
        item["rank"] = index
        item["ranking_note"] = (
            "Sorted by provider fare, then total journey duration, then provider-derived transfer time, then stops; missing fields are not estimated."
            if item["fare"] is not None and item["duration_minutes"] is not None
            else "Partial provider data: missing fare or duration is not fabricated; verify details with the provider."
        )
    return results


@router.get("/status")
def flight_provider_status():
    configured = bool(os.getenv(DRCT_API_KEY_ENV))
    if DRCT_SANDBOX:
        status = "sandbox_test_only"
        description = "Sandbox results are test data and are never labelled live."
    elif configured:
        status = "configured_live_provider"
        description = "Production API credentials are configured; each search makes a live provider request."
    else:
        status = "credentials_required"
        description = "No flight results are fabricated. Set STRANDEDPOOL_DRCT_API_KEY after obtaining provider access."
    return {
        "mode": "flight_search",
        "provider": "DRCT",
        "status": status,
        "api_key_configured": configured,
        "environment": "sandbox" if DRCT_SANDBOX else "production",
        "booking_enabled": False,
        "cancellation_enabled": False,
        "details": description,
    }


@router.get("/search")
async def search_flights(
    origin_airport: str = Query(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$"),
    destination_airport: str = Query(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$"),
    departure_date: date,
    adults: int = Query(default=1, ge=1, le=9),
    budget: float | None = Query(default=None, gt=0),
):
    origin = origin_airport.upper()
    destination = destination_airport.upper()
    if origin == destination:
        raise HTTPException(status_code=422, detail="Origin and destination airports must differ.")
    api_key = os.getenv(DRCT_API_KEY_ENV)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Flight provider credentials are not configured. No simulated flights, prices, schedules, or seat counts are returned. Obtain DRCT API access and set STRANDEDPOOL_DRCT_API_KEY on the server.",
        )

    request_body = {
        "flights": [{
            "departure_airport_code": origin,
            "arrival_airport_code": destination,
            "departure_date": departure_date.isoformat(),
        }],
        "passengers": [{"type": "ADT"} for _ in range(adults)],
    }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "DRCT-Version": DRCT_VERSION,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.post(f"{DRCT_BASE_URL}/offers_search", json=request_body, headers=headers)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Flight provider timed out; no fallback results were generated.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach the flight provider; no fallback results were generated.") from exc

    if response.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="Flight provider rejected the configured credentials. Check the server secret and provider environment.")
    if response.status_code == 429:
        raise HTTPException(status_code=503, detail="Flight provider rate limit reached; retry later. No fallback results were generated.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Flight provider returned HTTP {response.status_code}; no fallback results were generated.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Flight provider returned invalid JSON; no fallback results were generated.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Unexpected flight provider response; no fallback results were generated.")

    options = _normalise_options(payload)
    if budget is not None:
        for option in options:
            option["within_budget"] = option["fare"] is not None and option["currency"] == "INR" and option["fare"] <= budget
    return {
        "status": "ok",
        "provider": "DRCT",
        "environment": "sandbox" if DRCT_SANDBOX else "production",
        "data_mode": "SANDBOX_TEST_DATA" if DRCT_SANDBOX else "LIVE PROVIDER DATA",
        "origin_airport": origin,
        "destination_airport": destination,
        "departure_date": departure_date.isoformat(),
        "adults": adults,
        "result_count": len(options),
        "alternatives": options,
        "booking_completed": False,
        "note": "Offers are provider responses, not guaranteed seats. Reprice with the provider before purchase. This endpoint searches only; it does not create orders, issue tickets, cancel, or charge payment. Transfer duration and exact availability are shown only if provider fields expose them; they are never invented.",
    }


@router.post("/reprice/{offer_id}")
async def reprice_flight_offer(offer_id: str):
    """Ask DRCT to revalidate a previously returned offer before the traveller acts."""
    if not offer_id or len(offer_id) > 300:
        raise HTTPException(status_code=422, detail="Invalid provider offer ID.")
    api_key = os.getenv(DRCT_API_KEY_ENV)
    if not api_key:
        raise HTTPException(status_code=503, detail="Flight provider credentials are not configured; offer was not revalidated.")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "DRCT-Version": DRCT_VERSION,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.patch(
                f"{DRCT_BASE_URL}/offers/{offer_id}/price",
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Provider repricing timed out; offer remains unverified.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not revalidate offer with provider.") from exc
    if response.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="Provider rejected the configured credentials.")
    if response.status_code == 404:
        raise HTTPException(status_code=409, detail="Provider offer no longer exists; search again.")
    if response.status_code == 429:
        raise HTTPException(status_code=503, detail="Provider rate limit reached; retry later.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Provider repricing failed with HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Provider repricing returned invalid JSON.") from exc
    return {
        "provider": "DRCT",
        "provider_offer_id": offer_id,
        "data_mode": "SANDBOX_TEST_DATA" if DRCT_SANDBOX else "LIVE PROVIDER REPRICE",
        "verification": "SANDBOX_ONLY_NOT_REAL" if DRCT_SANDBOX else "REPRICED_BY_PROVIDER",
        "booking_completed": False,
        "provider_response": payload,
        "note": "This is a provider reprice response, not a booking or ticket. Confirm the provider's response and terms before proceeding.",
    }
