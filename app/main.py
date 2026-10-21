from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
import json
import os
import sqlite3
from pathlib import Path
from fastapi import FastAPI, HTTPException
import httpx
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
from app.flight_provider import router as flight_router, flight_provider_status

DB_PATH = Path(os.getenv("STRANDEDPOOL_DB", "strandedpool.db"))

app = FastAPI(
    title="StrandedPool Recovery",
    description="A working travel-disruption recovery MVP. Demo inventory is simulated, not live availability.",
    version="0.1.0",
)

# Flight search is a separate provider-backed path; demo planning remains explicitly simulated.
app.include_router(flight_router)

class Booking(BaseModel):
    origin: str = Field(default="Mysuru", min_length=1, max_length=80)
    destination: str = Field(default="Mangaluru", min_length=1, max_length=80)
    departure_time: str = Field(default="2026-10-21T09:00", description="Local ISO date-time")
    arrival_deadline: str = Field(default="2026-10-21T18:00", description="Local ISO date-time")
    budget_inr: int = Field(default=1500, ge=0, le=1000000)
    travellers: int = Field(default=1, ge=1, le=100)
    disruption: Literal["cancelled", "delayed", "weather"] = "cancelled"
    include_pooling: bool = True

class Alternative(BaseModel):
    mode: str
    provider: str
    departure: str
    arrival: str
    duration_minutes: int
    cost_per_person_inr: int
    capacity: int
    available_seats: int
    simulated: bool = True
    feasible: bool
    reasons: list[str]
    score: float

class RecoveryPlan(BaseModel):
    status: str
    data_mode: str
    original_journey: dict
    disruption_summary: str
    alternatives: list[Alternative]
    pool_proposals: list[dict]
    downstream_risks: list[dict]
    action_pack: list[dict]
    note: str

class ResolutionRequest(BaseModel):
    selected_provider: str = Field(min_length=1, max_length=100)
    decision_note: str = Field(default="", max_length=1000)

def db_connect():
    # Allow deployments to place the SQLite file in a dedicated data directory.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection

def init_db():
    with db_connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS saved_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                selected_provider TEXT,
                decision_note TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES saved_plans(id)
            )
        """)

init_db()

def parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Times must use local ISO format, e.g. 2026-10-21T09:00")

def build_plan(req: Booking) -> RecoveryPlan:
    if not req.origin.strip() or not req.destination.strip():
        raise ValueError("Origin and destination must not be blank")
    if req.origin.strip().casefold() == req.destination.strip().casefold():
        raise ValueError("Origin and destination must be different places")
    departure = parse_time(req.departure_time)
    deadline = parse_time(req.arrival_deadline)
    # Mixing local (naive) and timezone-aware timestamps makes comparisons
    # ambiguous and can otherwise surface as an unhandled server error.
    departure_is_aware = departure.utcoffset() is not None
    deadline_is_aware = deadline.utcoffset() is not None
    if departure_is_aware != deadline_is_aware:
        raise ValueError(
            "Use either local timestamps for both fields or timezone-aware timestamps for both fields"
        )
    if deadline <= departure:
        raise ValueError("Arrival deadline must be after departure time")

    # Fully reproducible demo options. These are deliberately labelled simulated.
    candidates = [
        {"mode":"Bus", "provider":"Demo Express", "offset":45, "duration":360, "cost":780, "capacity":40, "seats":18},
        {"mode":"Train", "provider":"Demo Rail", "offset":90, "duration":330, "cost":520, "capacity":300, "seats":0},
        {"mode":"Shared cab", "provider":"Demo Mobility", "offset":25, "duration":300, "cost":1450, "capacity":4, "seats":3},
        {"mode":"Bus", "provider":"Demo Nightliner", "offset":180, "duration":420, "cost":620, "capacity":36, "seats":12},
        {"mode":"Private cab", "provider":"Demo Taxi", "offset":15, "duration":285, "cost":5200, "capacity":4, "seats":4},
    ]
    alternatives = []
    for c in candidates:
        dep = departure + timedelta(minutes=c["offset"])
        arr = dep + timedelta(minutes=c["duration"])
        reasons = []
        if arr > deadline:
            reasons.append("Arrives after your deadline")
        if c["cost"] > req.budget_inr:
            reasons.append("Exceeds your budget")
        if c["seats"] < req.travellers:
            reasons.append(f"Only {c['seats']} demo seat(s) for {req.travellers} traveller(s)")
        feasible = not reasons
        # Lower cost and duration improve score; hard constraints remain separate.
        score = round(max(0.0, 100 - (c["cost"] / max(req.budget_inr, 1))*35 - c["duration"]/12), 1)
        alternatives.append(Alternative(
            mode=c["mode"], provider=c["provider"], departure=dep.isoformat(timespec="minutes"),
            arrival=arr.isoformat(timespec="minutes"), duration_minutes=c["duration"],
            cost_per_person_inr=c["cost"], capacity=c["capacity"], available_seats=c["seats"],
            feasible=feasible, reasons=reasons, score=score
        ))
    alternatives.sort(key=lambda a: (not a.feasible, -a.score, a.arrival))

    feasible = [a for a in alternatives if a.feasible]
    pools = []
    if req.include_pooling:
        for a in alternatives:
            if a.mode in ("Shared cab", "Private cab") and a.available_seats >= 2:
                pools.append({
                    "mode": a.mode, "provider": a.provider,
                    "suggested_group_size": min(a.available_seats, a.capacity),
                    "cost_per_person_inr": a.cost_per_person_inr,
                    "seats_remaining_after_request": max(0, a.available_seats-req.travellers),
                    "status": "SIMULATED PROPOSAL — no passengers have been contacted or booked"
                })

    downstream = [
        {"booking":"Hotel check-in", "risk":"High" if req.disruption == "cancelled" else "Medium",
         "suggested_action":"Notify the property of a possible late arrival; confirm its policy."},
        {"booking":"Pickup / onward connection", "risk":"High",
         "suggested_action":"Reconfirm pickup time and connection after selecting a recovery option."},
    ]
    actions = [
        {"title":"Hotel late-arrival message", "text":f"Hello, my journey from {req.origin} to {req.destination} has been disrupted. I may arrive later than planned. Could you please confirm the latest check-in time and whether my booking can be held?"},
        {"title":"Pickup change request", "text":f"My travel from {req.origin} to {req.destination} has been disrupted. Please let me update my pickup time once my replacement journey is confirmed."},
        {"title":"Refund / assistance request", "text":f"My original journey from {req.origin} to {req.destination} was affected by a {req.disruption} disruption. Please advise the available rebooking or refund options for my booking."},
    ]
    summary = {
        "cancelled":"The original service is treated as cancelled for this demo.",
        "delayed":"The original service is treated as delayed for this demo.",
        "weather":"Weather disruption is simulated; verify local advisories with official sources."
    }[req.disruption]
    return RecoveryPlan(
        status="alternatives_found" if feasible else "no_feasible_demo_option",
        data_mode="SIMULATED DEMO DATA",
        original_journey={"origin":req.origin, "destination":req.destination,
                          "departure":req.departure_time, "arrival_deadline":req.arrival_deadline,
                          "travellers":req.travellers, "budget_inr":req.budget_inr},
        disruption_summary=summary,
        alternatives=alternatives,
        pool_proposals=pools,
        downstream_risks=downstream,
        action_pack=actions,
        note="Transport candidates, prices, schedules and seats are simulated. Current city weather and estimated driving-route context are fetched from external services when available; neither is a public-transport schedule or official travel alert. Verify travel details with providers. This app does not book, cancel or message anyone."
    )

@app.post("/api/recovery/saved-plans")
def save_recovery_plan(req: Booking):
    """Create a demo recovery plan and persist it locally for later review."""
    try:
        plan = build_plan(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_connect() as connection:
        cursor = connection.execute(
            "INSERT INTO saved_plans(created_at, status, origin, destination, plan_json) VALUES (?, 'proposed', ?, ?, ?)",
            (now, req.origin, req.destination, plan.model_dump_json())
        )
        plan_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO audit_events(plan_id, created_at, event, detail) VALUES (?, ?, 'plan_created', ?)",
            (plan_id, now, "Generated a recovery proposal using simulated transport inventory.")
        )
    return {"plan_id": plan_id, "status": "proposed", "plan": plan}

@app.get("/api/recovery/saved-plans")
def list_saved_plans():
    """Return saved demo plans, newest first."""
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT id, created_at, status, origin, destination, selected_provider, decision_note FROM saved_plans ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return {"items": [dict(row) for row in rows]}

@app.get("/api/recovery/saved-plans/{plan_id}")
def get_saved_plan(plan_id: int):
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM saved_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Saved recovery plan not found")
        events = connection.execute(
            "SELECT created_at, event, detail FROM audit_events WHERE plan_id = ? ORDER BY id",
            (plan_id,)
        ).fetchall()
    item = dict(row)
    item["plan"] = json.loads(item.pop("plan_json"))
    item["audit_events"] = [dict(event) for event in events]
    return item

@app.post("/api/recovery/saved-plans/{plan_id}/resolve")
def resolve_saved_plan(plan_id: int, request: ResolutionRequest):
    """Record the traveller's chosen demo option. This does not book transport."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_connect() as connection:
        row = connection.execute("SELECT plan_json, status FROM saved_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Saved recovery plan not found")
        plan = json.loads(row["plan_json"])
        option = next((a for a in plan["alternatives"] if a["provider"] == request.selected_provider), None)
        if option is None:
            raise HTTPException(status_code=422, detail="Provider is not an alternative in this saved plan")
        if not option["feasible"]:
            raise HTTPException(status_code=422, detail="Cannot select an alternative that fails the demo constraints")
        connection.execute(
            "UPDATE saved_plans SET status = 'decision_recorded', selected_provider = ?, decision_note = ? WHERE id = ?",
            (request.selected_provider, request.decision_note, plan_id)
        )
        detail = json.dumps({"selected_provider": request.selected_provider, "note": request.decision_note})
        connection.execute(
            "INSERT INTO audit_events(plan_id, created_at, event, detail) VALUES (?, ?, 'traveller_decision_recorded', ?)",
            (plan_id, now, detail)
        )
    return {
        "plan_id": plan_id,
        "status": "decision_recorded",
        "selected_provider": request.selected_provider,
        "note": "Decision saved locally only. No transport was booked and no provider was contacted."
    }


@app.get("/api/recovery/saved-plans/{plan_id}/export.txt")
def export_saved_plan(plan_id: int):
    """Download a plain-text recovery summary; no booking is performed."""
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM saved_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Saved recovery plan not found")

    plan = json.loads(row["plan_json"])
    journey = plan.get("original_journey", {})
    lines = [
        "STRANDEDPOOL RECOVERY SUMMARY",
        "=" * 32,
        f"Plan ID: {row['id']}",
        f"Created (UTC): {row['created_at']}",
        f"Status: {row['status']}",
        f"Journey: {journey.get('origin', row['origin'])} -> {journey.get('destination', row['destination'])}",
        f"Departure: {journey.get('departure', 'Not provided')}",
        f"Arrival deadline: {journey.get('arrival_deadline', 'Not provided')}",
        f"Travellers: {journey.get('travellers', 'Not provided')}",
        f"Budget per traveller (INR): {journey.get('budget_inr', 'Not provided')}",
        "",
        "IMPORTANT: transport alternatives, prices, schedules, and seats are simulated demo data.",
        "This file is a planning aid only. No transport has been booked or cancelled.",
        "",
        "RANKED ALTERNATIVES",
        "-" * 20,
    ]
    for option in plan.get("alternatives", []):
        lines.extend([
            f"{option.get('mode')} — {option.get('provider')}",
            f"  Departure: {option.get('departure')} | Arrival: {option.get('arrival')}",
            f"  Duration: {option.get('duration_minutes')} min | Cost: INR {option.get('cost_per_person_inr')} per person",
            f"  Feasible against demo constraints: {'yes' if option.get('feasible') else 'no'}",
            f"  Demo seats: {option.get('available_seats')}",
        ])
        if option.get("reasons"):
            lines.append("  Reasons: " + "; ".join(option["reasons"]))
    lines.extend(["", "DOWNSTREAM REMINDERS", "-" * 20])
    for risk in plan.get("downstream_risks", []):
        lines.append(f"- {risk.get('booking')} ({risk.get('risk')}): {risk.get('suggested_action')}")
    lines.extend(["", "DRAFT ACTION MESSAGES", "-" * 21])
    for action in plan.get("action_pack", []):
        lines.extend([f"{action.get('title')}:", action.get("text", ""), ""])
    if row["selected_provider"]:
        lines.extend([
            "RECORDED CHOICE",
            "-" * 15,
            f"Selected demo option: {row['selected_provider']}",
            f"Decision note: {row['decision_note'] or ''}",
            "This is a recorded preference, not a reservation.",
        ])
    lines.extend(["", "Verify schedules, fares, seats, and advisories directly with official providers before travel."])
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": f'attachment; filename="strandedpool-recovery-{plan_id}.txt"'},
    )

@app.get("/api/recovery/saved-plans/{plan_id}/audit")
def get_plan_audit(plan_id: int):
    with db_connect() as connection:
        exists = connection.execute("SELECT id FROM saved_plans WHERE id = ?", (plan_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Saved recovery plan not found")
        events = connection.execute(
            "SELECT created_at, event, detail FROM audit_events WHERE plan_id = ? ORDER BY id",
            (plan_id,)
        ).fetchall()
    return {"plan_id": plan_id, "events": [dict(event) for event in events]}

@app.get("/health")
def health():
    """Liveness check: the process is running."""
    return {"ok": True, "service": "StrandedPool Recovery", "data_mode": "SIMULATED DEMO DATA"}

@app.get("/health/ready")
def readiness():
    """Readiness check: the app can reach its configured SQLite database."""
    try:
        with db_connect() as connection:
            connection.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="Database is not ready") from exc
    return {
        "ok": True,
        "database": "reachable",
        "data_mode": "SIMULATED DEMO DATA",
    }

@app.get("/api/providers/status")
def provider_status():
    """Report integration capability without claiming unverified connectivity."""
    return {
        "flight_search": flight_provider_status(),
        "transport_inventory": {
            "status": "simulated_only",
            "live_data_available": False,
            "details": "Transport candidates, prices, schedules, and seats are demo fixtures.",
        },
        "current_weather": {
            "status": "implemented_on_demand",
            "provider": "Open-Meteo",
            "api_key_required": False,
            "connectivity": "not_checked_until_request",
        },
        "weather_forecast": {
            "status": "implemented_on_demand",
            "provider": "Open-Meteo",
            "maximum_days": 7,
            "data_type": "city_level_forecast_not_official_alert",
        },
        "driving_route_context": {
            "status": "implemented_on_demand",
            "providers": ["Open-Meteo Geocoding", "OSRM public routing service"],
            "data_type": "estimated_driving_route_not_public_transport",
            "connectivity": "not_checked_until_request",
        },
        "booking_site_handoff": {
            "status": "implemented",
            "integration_type": "external_website_handoff",
            "inventory_checked": False,
            "booking_completed": False,
            "supported_modes": ["bus", "train", "flight", "shared cab", "private cab"],
        },
        "booking_actions": {
            "status": "not_implemented",
            "can_book": False,
            "can_cancel": False,
            "can_message_providers": False,
        },
    }

WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

@app.get("/api/weather/current")
async def current_weather(city: str):
    """Fetch current conditions for a city from Open-Meteo; never substitutes demo weather."""
    city = city.strip()
    if not city or len(city) > 100:
        raise HTTPException(status_code=422, detail="City must be between 1 and 100 characters")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            geo_response = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
            )
            geo_response.raise_for_status()
            locations = geo_response.json().get("results") or []
            if not locations:
                raise HTTPException(status_code=404, detail=f"No location found for '{city}'")

            location = locations[0]
            weather_response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                    "current": (
                        "temperature_2m,relative_humidity_2m,apparent_temperature,"
                        "is_day,precipitation,rain,showers,snowfall,weather_code,wind_speed_10m"
                    ),
                    "timezone": "auto",
                },
            )
            weather_response.raise_for_status()
            current = weather_response.json().get("current")
            if not current:
                raise HTTPException(status_code=502, detail="Weather provider returned no current conditions")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Live weather provider is temporarily unavailable; no weather estimate was substituted",
        ) from exc

    code = current.get("weather_code")
    return {
        "data_mode": "LIVE WEATHER DATA",
        "provider": "Open-Meteo",
        "location": {
            "name": location.get("name", city),
            "country": location.get("country"),
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        },
        "observed_at": current.get("time"),
        "current": {
            "condition": WEATHER_CODES.get(code, "Unknown conditions"),
            "weather_code": code,
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "relative_humidity_percent": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "is_day": current.get("is_day"),
        },
        "note": (
            "These are current conditions at the matched city, not a route forecast or official travel alert. "
            "Check official weather and transport advisories before travelling."
        ),
    }

@app.get("/api/route/driving")
async def driving_route(origin: str, destination: str):
    """Return estimated road distance and duration; this is not transit inventory."""
    origin = origin.strip()
    destination = destination.strip()
    if not origin or not destination:
        raise HTTPException(status_code=422, detail="Origin and destination are required")
    if len(origin) > 100 or len(destination) > 100:
        raise HTTPException(status_code=422, detail="Origin and destination must be 100 characters or fewer")
    if origin.casefold() == destination.casefold():
        raise HTTPException(status_code=422, detail="Origin and destination must be different places")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            locations = []
            for city in (origin, destination):
                geocode_response = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": city, "count": 1, "language": "en", "format": "json"},
                )
                geocode_response.raise_for_status()
                matches = geocode_response.json().get("results") or []
                if not matches:
                    raise HTTPException(status_code=404, detail=f"No location found for '{city}'")
                locations.append(matches[0])

            start, end = locations
            route_url = (
                "https://router.project-osrm.org/route/v1/driving/"
                f"{start['longitude']},{start['latitude']};"
                f"{end['longitude']},{end['latitude']}"
            )
            route_response = await client.get(
                route_url,
                params={"overview": "false", "alternatives": "false", "steps": "false"},
            )
            route_response.raise_for_status()
            route_data = route_response.json()
            routes = route_data.get("routes") or []
            if route_data.get("code") != "Ok" or not routes:
                raise HTTPException(status_code=404, detail="No driving route was returned for these locations")
            route = routes[0]
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Driving-route provider is temporarily unavailable; no route estimate was substituted",
        ) from exc

    return {
        "data_mode": "LIVE ROUTING ESTIMATE",
        "provider": "OSRM public routing service",
        "origin": {
            "name": start.get("name", origin),
            "country": start.get("country"),
            "latitude": start["latitude"],
            "longitude": start["longitude"],
        },
        "destination": {
            "name": end.get("name", destination),
            "country": end.get("country"),
            "latitude": end["latitude"],
            "longitude": end["longitude"],
        },
        "distance_km": round(route["distance"] / 1000, 1),
        "estimated_driving_minutes": round(route["duration"] / 60),
        "note": (
            "This is an estimated road-driving route, not a public-transport timetable, "
            "traffic-aware arrival guarantee, or official travel advisory."
        ),
    }


@app.get("/api/weather/forecast")
async def weather_forecast(city: str, days: int = 3):
    """Fetch a short daily forecast from Open-Meteo; never substitutes demo conditions."""
    city = city.strip()
    if not city or len(city) > 100:
        raise HTTPException(status_code=422, detail="City must be between 1 and 100 characters")
    if days < 1 or days > 7:
        raise HTTPException(status_code=422, detail="Forecast days must be between 1 and 7")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            geo_response = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
            )
            geo_response.raise_for_status()
            locations = geo_response.json().get("results") or []
            if not locations:
                raise HTTPException(status_code=404, detail=f"No location found for '{city}'")

            location = locations[0]
            forecast_response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                    "daily": (
                        "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max,precipitation_sum,wind_speed_10m_max"
                    ),
                    "forecast_days": days,
                    "timezone": "auto",
                },
            )
            forecast_response.raise_for_status()
            daily = forecast_response.json().get("daily")
            if not isinstance(daily, dict) or not daily.get("time"):
                raise HTTPException(status_code=502, detail="Weather provider returned no daily forecast")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Live weather forecast provider is temporarily unavailable; no forecast was substituted",
        ) from exc

    fields = (
        "temperature_2m_max", "temperature_2m_min",
        "precipitation_probability_max", "precipitation_sum", "wind_speed_10m_max",
    )
    forecast = []
    for index, day in enumerate(daily["time"][:days]):
        codes = daily.get("weather_code") or []
        code = codes[index] if index < len(codes) else None
        item = {"date": day, "condition": WEATHER_CODES.get(code, "Unknown conditions")}
        for key in fields:
            values = daily.get(key) or []
            item[key] = values[index] if index < len(values) else None
        forecast.append(item)

    return {
        "data_mode": "LIVE WEATHER FORECAST",
        "provider": "Open-Meteo",
        "location": {
            "name": location.get("name", city),
            "country": location.get("country"),
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        },
        "forecast_days": forecast,
        "note": (
            "This is a city-level forecast, not an official severe-weather warning or a route-specific forecast. "
            "Check official advisories and transport providers before travelling."
        ),
    }


BOOKING_SITE_HANDOFFS = {
    "bus": {"provider": "redBus", "url": "https://www.redbus.in/", "note": "Enter the journey and date on redBus and verify live fares and seats there."},
    "train": {"provider": "IRCTC", "url": "https://www.irctc.co.in/nget/train-search", "note": "Search trains and confirm availability on the official IRCTC site."},
    "flight": {"provider": "MakeMyTrip", "url": "https://www.makemytrip.com/flights/", "note": "Enter the airports, date, and passenger count and confirm the final fare before payment."},
    "shared cab": {"provider": "BlaBlaCar", "url": "https://www.blablacar.in/", "note": "Check whether a matching shared ride is offered for this route and date."},
    "private cab": {"provider": "Uber", "url": "https://www.uber.com/in/en/ride/", "note": "Enter pickup and destination in Uber to check current service and price."},
    "cab": {"provider": "Uber", "url": "https://www.uber.com/in/en/ride/", "note": "Enter pickup and destination in Uber to check current service and price."},
}


@app.get("/api/booking/sites")
def booking_sites(mode: str, origin: str, destination: str, departure_date: str = ""):
    """Return a safe handoff to a real booking website; never claims to check or book inventory."""
    normalized_mode = mode.strip().casefold()
    if not normalized_mode:
        raise HTTPException(status_code=422, detail="Transport mode is required")
    if not origin.strip() or not destination.strip():
        raise HTTPException(status_code=422, detail="Origin and destination are required")
    if origin.strip().casefold() == destination.strip().casefold():
        raise HTTPException(status_code=422, detail="Origin and destination must differ")
    site = BOOKING_SITE_HANDOFFS.get(normalized_mode)
    if site is None:
        raise HTTPException(status_code=404, detail="No external booking-site handoff is configured for this mode")
    return {
        "mode": normalized_mode,
        "provider": site["provider"],
        "booking_url": site["url"],
        "journey": {"origin": origin.strip(), "destination": destination.strip(), "departure_date": departure_date.strip() or None},
        "integration_type": "external_website_handoff",
        "inventory_checked": False,
        "booking_completed": False,
        "note": site["note"] + " This opens the provider website; it does not transfer your details or book a ticket.",
    }


@app.post("/api/recovery/plan", response_model=RecoveryPlan)
def recovery_plan(req: Booking):
    try:
        return build_plan(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StrandedPool | Travel recovery</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141d31;--muted:#a7b4ca;--line:#293752;--accent:#77e0c1;--warn:#ffd27d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0%,#1b3150 0,transparent 40%),var(--bg);font:15px/1.5 system-ui,sans-serif;color:#eef4ff}
main{max-width:1100px;margin:auto;padding:28px 18px 60px}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:12px}
h1{font-size:clamp(32px,6vw,54px);line-height:1.05;margin:10px 0}p{color:var(--muted)}.notice{border:1px solid #806b36;background:#302817;color:#ffe3a4;padding:12px 14px;border-radius:12px;margin:22px 0}
.layout{display:grid;grid-template-columns:320px 1fr;gap:18px}.panel{background:#111a2b;border:1px solid var(--line);border-radius:18px;padding:18px;min-width:0}
label{display:block;color:#c7d3e6;font-size:13px;margin:12px 0 5px}input,select{width:100%;padding:11px;border-radius:9px;border:1px solid #35435d;background:#0b1323;color:#fff}
button{margin-top:16px;width:100%;border:0;border-radius:10px;background:var(--accent);color:#09241d;font-weight:800;padding:13px;cursor:pointer}button:disabled{opacity:.6}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.option{border:1px solid var(--line);border-radius:14px;padding:14px;background:#0d1626}.option.best{border-color:var(--accent)}.tag{display:inline-block;font-size:11px;color:#ffd27d;border:1px solid #806b36;border-radius:30px;padding:3px 7px;margin:2px 0 7px}
.money{font-size:25px;font-weight:800}.muted{color:var(--muted);font-size:13px}.good{color:var(--accent)}.bad{color:#ff9d9d}.section{margin-top:20px}ul{padding-left:20px}.action{border-top:1px solid var(--line);padding:12px 0}.action button{width:auto;margin:8px 0 0;padding:8px 12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0a1220;border-radius:9px;padding:10px;color:#d7e4f7}
@media(max-width:760px){.layout{grid-template-columns:1fr}.cards{grid-template-columns:1fr}}
</style></head><body><main>
<div class="eyebrow">Predict · Pool · Recover</div><h1>Your journey changed.<br>Let's find a way forward.</h1>
<p>Travel-disruption recovery prototype with constraint-aware alternatives, group pooling ideas and ready-to-review messages.</p>
<div class="notice"><strong>Prototype mode:</strong> all fares, seats, operators and schedules below are simulated. Current weather and estimated road-route context are fetched separately when available. Nothing is booked.</div>
<section class="panel section" id="live-flights">
<h2>Live flight search · DRCT</h2>
<p>Uses the flight provider API when server credentials are configured. A search result is not a confirmed seat; use provider repricing before making a decision. Airport codes are IATA codes.</p>
<form id="flight-form">
<label for="flight-origin">Origin airport</label><input id="flight-origin" name="origin_airport" value="BLR" pattern="[A-Za-z]{3}" maxlength="3" required>
<label for="flight-destination">Destination airport</label><input id="flight-destination" name="destination_airport" value="DEL" pattern="[A-Za-z]{3}" maxlength="3" required>
<label for="flight-date">Departure date</label><input id="flight-date" name="departure_date" type="date" required>
<label for="flight-adults">Adults</label><input id="flight-adults" name="adults" type="number" min="1" max="9" value="1" required>
<button id="flight-submit" type="submit">Search provider flights</button>
</form><p id="flight-error" class="bad" role="alert"></p><div id="flight-results" class="cards"></div>
</section>
<div class="layout"><section class="panel"><h2>Your disrupted trip</h2>
<form id="form">
<label>From</label><input name="origin" value="Mysuru" required maxlength="80">
<label>To</label><input name="destination" value="Mangaluru" required maxlength="80">
<label>Original departure (local time)</label><input name="departure_time" type="datetime-local" value="2026-10-21T09:00" required>
<label>Must arrive by</label><input name="arrival_deadline" type="datetime-local" value="2026-10-21T18:00" required>
<label>Budget per traveller (₹)</label><input name="budget_inr" type="number" min="0" max="1000000" value="1500" required>
<label>Travellers</label><input name="travellers" type="number" min="1" max="100" value="2" required>
<label>Disruption type</label><select name="disruption"><option value="cancelled">Cancelled</option><option value="delayed">Delayed</option><option value="weather">Weather disruption</option></select>
<label><input type="checkbox" name="include_pooling" checked style="width:auto"> Include shared-ride proposals</label>
<button id="submit">Build recovery plan ↗</button><p id="error" role="alert"></p>
</form></section><section class="panel" id="results"><h2>Recovery workspace</h2><p>Enter your trip details and build a plan to see ranked alternatives, pooling proposals, downstream risks and message drafts.</p><div class="muted">No sign-in or API key required for the local demo.</div></section></div>
</main><script>
const form=document.querySelector('#form'), results=document.querySelector('#results'), err=document.querySelector('#error'), btn=document.querySelector('#submit'); let lastPayload=null, currentPlanId=null, decisionInProgress=false;
const money=n=>'₹'+Number(n).toLocaleString('en-IN');
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
form.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';btn.disabled=true;btn.textContent='Building plan…';
const f=new FormData(form), body={origin:f.get('origin'),destination:f.get('destination'),departure_time:f.get('departure_time'),arrival_deadline:f.get('arrival_deadline'),budget_inr:Number(f.get('budget_inr')),travellers:Number(f.get('travellers')),disruption:f.get('disruption'),include_pooling:f.get('include_pooling')==='on'}; lastPayload=body;
try{const res=await fetch('/api/recovery/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await res.json();if(!res.ok)throw new Error(data.detail?.[0]?.msg||'Could not build plan');render(data)}catch(e){err.textContent=e.message||'Request failed'}finally{btn.disabled=false;btn.textContent='Build recovery plan ↗'}});
function render(d){currentPlanId=null;const cards=d.alternatives.map((a,i)=>'<article class="option '+(i===0&&a.feasible?'best':'')+'"><span class="tag">'+(a.feasible?'CONSTRAINTS PASS':'NOT FEASIBLE')+'</span><h3>'+esc(a.mode)+' · '+esc(a.provider)+'</h3><div class="money">'+money(a.cost_per_person_inr)+'<span class="muted"> / person</span></div><p>'+esc(a.departure.replace('T',' '))+' → '+esc(a.arrival.replace('T',' '))+'</p><p>'+a.duration_minutes+' min · '+a.available_seats+' simulated seats</p>'+(a.reasons.length?'<ul class="bad">'+a.reasons.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'<p class="good">Fits budget, deadline and demo capacity.</p><button type="button" data-choose-provider="'+esc(a.provider)+'">Record this choice</button>')+'<button type="button" data-book-mode="'+esc(a.mode)+'" data-book-origin="'+esc(d.original_journey.origin)+'" data-book-destination="'+esc(d.original_journey.destination)+'" data-book-date="'+esc(d.original_journey.departure_time.slice(0,10))+'">Check real booking site</button><p class="muted">Score '+a.score+'/100 · simulated inventory</p><p class="muted">External site only: demo fares and seats are not live.</p></article>').join('');
const pools=d.pool_proposals.length?d.pool_proposals.map(p=>'<div class="action"><strong>'+esc(p.mode)+' · '+esc(p.provider)+'</strong><p>Suggested group: '+p.suggested_group_size+' · '+money(p.cost_per_person_inr)+' per person</p><p class="muted">'+esc(p.status)+'</p></div>').join(''):'<p>No shared ride proposals in this scenario.</p>';
const risks=d.downstream_risks.map(r=>'<li><strong>'+esc(r.booking)+' — '+esc(r.risk)+'</strong><br>'+esc(r.suggested_action)+'</li>').join('');
const actions=d.action_pack.map((a,i)=>'<div class="action"><strong>'+esc(a.title)+'</strong><pre id="msg'+i+'">'+esc(a.text)+'</pre><button type="button" onclick="copyMsg('+i+')">Copy message</button></div>').join('');
results.innerHTML='<h2>Recovery plan</h2><p class="muted">'+esc(d.disruption_summary)+' · '+esc(d.status)+'</p><div class="tag">'+esc(d.data_mode)+'</div><div class="section"><h3>Estimated driving route</h3><p class="muted">Road distance and typical route duration only—not public-transport availability or live traffic.</p><div id="driving-route" class="cards"><p>Loading route estimate…</p></div></div><div class="section"><h3>Current weather context</h3><p class="muted">Live conditions at the matched origin and destination locations. This is not a route forecast.</p><div id="weather" class="cards"><p>Loading live weather…</p></div></div><div class="section"><h3>3-day weather outlook</h3><p class="muted">City-level forecast for both ends of your journey; not an official warning.</p><div id="weather-forecast" class="cards"><p>Loading forecast…</p></div></div><h3>Ranked alternatives</h3><div class="cards">'+cards+'</div><div class="section"><h3>Pooling proposals</h3>'+pools+'</div><div class="section"><h3>Downstream booking risks</h3><ul>'+risks+'</ul></div><div class="section"><h3>Action pack</h3>'+actions+'</div><div class="notice">'+esc(d.note)+'</div><button type="button" onclick="savePlan()">Save plan to recovery history</button><button type="button" onclick="loadHistory()">View saved plans</button><div id="history"></div>';loadCurrentWeather(d.original_journey.origin,d.original_journey.destination);loadDrivingRoute(d.original_journey.origin,d.original_journey.destination);loadWeatherForecast(d.original_journey.origin,d.original_journey.destination)}
window.loadDrivingRoute=async(origin,destination)=>{const target=document.querySelector('#driving-route');if(!target)return;try{const r=await fetch('/api/route/driving?origin='+encodeURIComponent(origin)+'&destination='+encodeURIComponent(destination));const d=await r.json();if(!r.ok)throw new Error(d.detail||'Route provider unavailable');target.innerHTML='<article class="option"><span class="tag">LIVE ROUTING ESTIMATE · '+esc(d.provider)+'</span><h3>'+esc(d.origin.name)+' → '+esc(d.destination.name)+'</h3><div class="money">'+esc(d.distance_km)+' km</div><p>Estimated driving time: '+esc(d.estimated_driving_minutes)+' min</p><p class="muted">'+esc(d.note)+'</p></article>'}catch(e){target.innerHTML='<article class="option"><p class="bad">'+esc(e.message||'Driving-route provider unavailable')+'</p><p class="muted">No route estimate was substituted.</p></article>'}};
window.loadCurrentWeather=async(origin,destination)=>{const target=document.querySelector('#weather');if(!target)return;const cities=[...new Set([origin,destination])];const items=await Promise.all(cities.map(async city=>{try{const r=await fetch('/api/weather/current?city='+encodeURIComponent(city));const data=await r.json();return r.ok?data:{error:data.detail||'Weather provider unavailable',city}}catch(e){return {error:'Could not connect to the live weather endpoint',city}}}));target.innerHTML=items.map(w=>{if(w.error)return '<article class="option"><h3>'+esc(w.city)+'</h3><p class="bad">'+esc(w.error)+'</p><p class="muted">No demo weather substituted.</p></article>';const v=w.current;return '<article class="option"><span class="tag">LIVE · '+esc(w.provider)+'</span><h3>'+esc(w.location.name)+(w.location.country?', '+esc(w.location.country):'')+'</h3><div class="money">'+esc(v.temperature_c)+'°C</div><p>'+esc(v.condition)+' · feels like '+esc(v.feels_like_c)+'°C</p><p>'+esc(v.relative_humidity_percent)+'% humidity · wind '+esc(v.wind_speed_kmh)+' km/h</p><p>Precipitation: '+esc(v.precipitation_mm)+' mm</p><p class="muted">Observed: '+esc(w.observed_at||'time not provided')+'</p></article>'}).join('')+'<p class="muted">Weather source: Open-Meteo. Current city conditions do not replace official alerts or a route forecast.</p>'};
window.loadWeatherForecast=async(origin,destination)=>{const target=document.querySelector('#weather-forecast');if(!target)return;const cities=[...new Set([origin,destination])];const items=await Promise.all(cities.map(async city=>{try{const r=await fetch('/api/weather/forecast?city='+encodeURIComponent(city)+'&days=3');const data=await r.json();return r.ok?data:{error:data.detail||'Forecast provider unavailable',city}}catch(e){return {error:'Could not connect to the forecast endpoint',city}}}));target.innerHTML=items.map(w=>{if(w.error)return '<article class="option"><h3>'+esc(w.city)+'</h3><p class="bad">'+esc(w.error)+'</p><p class="muted">No forecast was substituted.</p></article>';return '<article class="option"><span class="tag">LIVE FORECAST · '+esc(w.provider)+'</span><h3>'+esc(w.location.name)+(w.location.country?', '+esc(w.location.country):'')+'</h3>'+w.forecast_days.map(d=>'<div class="action"><strong>'+esc(d.date)+' · '+esc(d.condition)+'</strong><p>'+esc(d.temperature_2m_min)+'°C to '+esc(d.temperature_2m_max)+'°C</p><p>Precipitation chance: '+esc(d.precipitation_probability_max??'N/A')+'% · rainfall '+esc(d.precipitation_sum??'N/A')+' mm</p><p>Max wind: '+esc(d.wind_speed_10m_max??'N/A')+' km/h</p></div>').join('')+'<p class="muted">'+esc(w.note)+'</p></article>'}).join('')};
window.savePlan=async()=>{if(!lastPayload){alert('Build a plan first');return}try{const r=await fetch('/api/recovery/saved-plans',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(lastPayload)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Save failed');currentPlanId=d.plan_id;alert('Saved recovery plan #'+d.plan_id);await loadHistory()}catch(e){alert(e.message||'Save failed')}};window.chooseOption=async(provider)=>{if(decisionInProgress)return;if(!lastPayload){alert('Build a plan first');return}decisionInProgress=true;try{if(!currentPlanId){const saved=await fetch('/api/recovery/saved-plans',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(lastPayload)});const savedData=await saved.json();if(!saved.ok)throw new Error(savedData.detail||'Could not save recovery plan');currentPlanId=savedData.plan_id}const response=await fetch('/api/recovery/saved-plans/'+currentPlanId+'/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_provider:provider,decision_note:'Choice recorded from the recovery workspace UI'})});const data=await response.json();if(!response.ok)throw new Error(data.detail||'Could not record choice');alert('Choice recorded for recovery plan #'+currentPlanId+'. No transport was booked.');await loadHistory()}catch(e){alert(e.message||'Could not record choice')}finally{decisionInProgress=false}};
results.addEventListener('click',event=>{const button=event.target.closest('[data-choose-provider]');if(button)window.chooseOption(button.dataset.chooseProvider)});window.openBookingSite=async(mode,origin,destination,date)=>{const tab=window.open('about:blank','_blank');if(!tab){alert('Allow pop-ups for this app to open the booking provider.');return}try{tab.opener=null;tab.document.title='Opening booking provider…';const params=new URLSearchParams({mode,origin,destination,departure_date:date||''});const response=await fetch('/api/booking/sites?'+params.toString());const data=await response.json();if(!response.ok)throw new Error(data.detail||'No booking site configured');const approvedHosts=new Set(['www.redbus.in','www.irctc.co.in','www.makemytrip.com','www.blablacar.in','www.uber.com']);const url=new URL(data.booking_url);if(url.protocol!=='https:'||!approvedHosts.has(url.hostname))throw new Error('Booking site was not on the approved provider list');tab.location.href=url.href;alert(data.provider+': '+data.note)}catch(e){tab.close();alert(e.message||'Could not open booking site')}};
results.addEventListener('click',event=>{const button=event.target.closest('[data-book-mode]');if(button)window.openBookingSite(button.dataset.bookMode,button.dataset.bookOrigin,button.dataset.bookDestination,button.dataset.bookDate)});


window.loadHistory=async()=>{const target=document.querySelector('#history');if(!target)return;target.innerHTML='<p>Loading saved plans…</p>';try{const r=await fetch('/api/recovery/saved-plans');const d=await r.json();target.innerHTML='<h3>Saved recovery history</h3>'+(d.items.length?d.items.map(p=>'<div class="action"><strong>#'+p.id+' · '+esc(p.origin)+' → '+esc(p.destination)+'</strong><p>'+esc(p.status)+' · '+esc(p.created_at)+'</p>'+(p.selected_provider?'<p>Selected demo option: '+esc(p.selected_provider)+'</p>':'')+'<a href="/api/recovery/saved-plans/'+p.id+'" target="_blank" rel="noreferrer">Open plan details and audit log</a> · <a href="/api/recovery/saved-plans/'+p.id+'/export.txt">Download summary</a></div>').join(''):'<p>No saved plans yet.</p>')}catch(e){target.innerHTML='<p>Could not load saved history.</p>'}};
window.copyMsg=async i=>{const t=document.querySelector('#msg'+i).textContent;try{await navigator.clipboard.writeText(t);alert('Message copied')}catch(e){alert('Copy failed — select the text and copy manually')}};

const flightForm=document.querySelector('#flight-form'), flightResults=document.querySelector('#flight-results'), flightError=document.querySelector('#flight-error');
document.querySelector('#flight-date').value=new Date(Date.now()+7*86400000).toISOString().slice(0,10);
flightForm.addEventListener('submit',async e=>{e.preventDefault();flightError.textContent='';flightResults.innerHTML='<p>Contacting flight provider…</p>';const f=new FormData(flightForm);const q=new URLSearchParams({origin_airport:String(f.get('origin_airport')).toUpperCase(),destination_airport:String(f.get('destination_airport')).toUpperCase(),departure_date:f.get('departure_date'),adults:f.get('adults')});try{const r=await fetch('/api/flights/search?'+q.toString());const d=await r.json();if(!r.ok)throw new Error(d.detail||'Flight search failed');flightResults.innerHTML=d.alternatives.length?d.alternatives.map(a=>'<article class="option"><span class="tag">'+esc(d.data_mode)+'</span><h3>Option '+a.rank+' · '+esc(a.provider_offer_id)+'</h3><div class="money">'+(a.fare===null?'Fare not supplied':esc(a.currency||'')+' '+esc(a.fare))+'</div><p>'+(a.duration_minutes===null?'Duration not supplied':esc(a.duration_minutes)+' minutes')+' · '+(a.stops===null?'Stops unknown':esc(a.stops)+' stops')+'</p><p>Availability: '+esc(a.availability_status)+'</p><p class="muted">'+esc(a.ranking_note)+'</p>'+(String(a.provider_offer_id).startsWith('response-option-')?'':'<button type="button" data-reprice="'+esc(a.provider_offer_id)+'">Recheck price with provider</button>')+'<pre hidden id="reprice-'+esc(a.provider_offer_id)+'"></pre></article>').join(''):'<p>No offers returned by provider for this route/date. No demo options were substituted.</p>';flightResults.insertAdjacentHTML('beforeend','<p class="muted">'+esc(d.note)+'</p>')}catch(err){flightResults.innerHTML='';flightError.textContent=err.message||'Flight provider unavailable; no simulated flights were substituted.'}});
flightResults.addEventListener('click',async e=>{const b=e.target.closest('[data-reprice]');if(!b)return;b.disabled=true;b.textContent='Rechecking…';const id=b.dataset.reprice;try{const r=await fetch('/api/flights/reprice/'+encodeURIComponent(id),{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Provider repricing failed');const target=document.querySelector('#reprice-'+CSS.escape(id));target.hidden=false;target.textContent=JSON.stringify(d.provider_response,null,2);b.textContent=d.verification}catch(err){b.textContent='Retry provider recheck';alert(err.message||'Could not reprice offer')}finally{b.disabled=false}});

</script></body></html>""")
