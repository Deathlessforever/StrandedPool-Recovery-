# StrandedPool — Predict. Pool. Recover.

A working, original MVP inspired by the problem and feature ideas described in [MAfshaan17/StrandedPool](https://github.com/MAfshaan17/StrandedPool): travel disruption recovery, ranked alternatives, shared-ride proposals, downstream booking checks, and an action pack.

This repository is an independent implementation. The original repository did not expose a LICENSE file when checked, so this project does not copy its source code. It implements the publicly described product concept in its own code.

## What works now

- Responsive browser UI for entering an affected journey.
- FastAPI backend with /health liveness, /health/ready database readiness, POST /api/recovery/plan, and a live current-weather endpoint.
- Alternative ranking against budget, arrival deadline, and number of travellers.
- Live current weather for the matched origin and destination cities via Open-Meteo, with provider failures shown instead of fabricated weather.
- Live 3-day city-level weather outlook for the origin and destination, including temperature range, precipitation chance, rainfall, and maximum wind, with no fabricated fallback.
- Estimated road-driving distance and duration from Open-Meteo geocoding and the OSRM public routing service, clearly separated from simulated public-transport options.
- Provider-status endpoint that clearly distinguishes simulated transport inventory, on-demand weather integration, and unavailable booking actions.
- Simulated capacity checks and shared-ride pooling proposals.
- Downstream booking risk reminders for hotels and onward pickups.
- Copyable draft messages for hotel, pickup, and refund/assistance requests.
- SQLite persistence for saved recovery plans, selected demo options, and an audit trail.
- Safe decision recording: rejects options that fail the model's constraints.
- The browser UI lets travellers record a feasible option; it saves the proposal and decision to local SQLite and clearly confirms that no booking is made.
- API documentation, input validation, and automated tests in GitHub Actions.

## Important data boundary

**Transport providers, schedules, fares, seats, and alternatives are simulated demo data.** The app fetches current conditions for matched cities from Open-Meteo and estimated road-driving distance/duration from OSRM. Driving estimates are not public-transport schedules and are not guaranteed to reflect live traffic. The app does not provide route weather forecasts, official severe-weather alerts, live rail/bus/flight inventory, hotel availability, or verified seat counts. Provider failures are shown as errors; demo weather or route estimates are never substituted. It does not book, cancel, send messages, or process refunds. Confirm details directly with the relevant provider before travelling.

The demo transport data exists to make the workflow usable and testable while authorized provider APIs and partnerships are evaluated. Current city weather and road-driving route estimates are fetched from external services on demand. Live transport availability, payment, booking, and automated rebooking are not implemented.

## Run locally

Requires Python 3.11+.

~~~bash
git clone https://github.com/Deathlessforever/StrandedPool-Recovery-.git
cd StrandedPool-Recovery-
python -m venv .venv
~~~

Activate the environment:

**Windows PowerShell**
~~~powershell
.\.venv\Scripts\Activate.ps1
~~~

**macOS / Linux**
~~~bash
source .venv/bin/activate
~~~

Install and start:

~~~bash
pip install -r requirements.txt
uvicorn app.main:app --reload
~~~

Open http://127.0.0.1:8000 for the web app and http://127.0.0.1:8000/docs for interactive API docs.

## Run with Docker

Docker and Docker Compose are required.

~~~bash
docker compose up --build
~~~

Open http://localhost:8000. Compose mounts a named volume for SQLite so saved demo plans survive container recreation. To stop the app:

~~~bash
docker compose down
~~~

This is a local deployment configuration. A public production deployment still needs a host, persistent storage, monitoring, HTTPS, privacy controls, and verified provider integrations.

## API example

~~~bash
curl -X POST http://127.0.0.1:8000/api/recovery/plan \
  -H "Content-Type: application/json" \
  -d '{
    "origin": "Mysuru",
    "destination": "Mangaluru",
    "departure_time": "2026-10-21T09:00",
    "arrival_deadline": "2026-10-21T18:00",
    "budget_inr": 1500,
    "travellers": 2,
    "disruption": "cancelled",
    "include_pooling": true
  }'
~~~

Response includes ranked alternatives, feasibility reasons, pool proposals, downstream risks, and draft messages. Every alternative has simulated=true and the response identifies its data mode.

### Provider status

`GET /api/providers/status` reports what is actually implemented. Transport inventory is marked simulated-only, current weather and driving-route context are marked implemented on demand, and booking/cancellation/provider messaging are explicitly marked not implemented.

### Driving route context

`GET /api/route/driving?origin=Mysuru&destination=Mangaluru` geocodes both places and requests a road route from the OSRM public routing service. It returns distance and estimated driving duration. This is not a public-transport timetable, not a traffic-aware arrival guarantee, and not an official travel advisory. Provider errors are returned rather than replaced with fabricated route values.

### Weather forecast

`GET /api/weather/forecast?city=Mysuru&days=3` returns a live daily forecast for 1–7 days from Open-Meteo, including minimum/maximum temperature, precipitation probability and total, weather condition, and maximum wind. The UI requests a 3-day outlook for both journey cities. This is city-level forecast data, not an official warning or route-specific forecast. Provider errors are surfaced instead of substituted with demo values.

### Current weather

`GET /api/weather/current?city=Mysuru` geocodes the supplied city and returns current conditions from Open-Meteo. The response labels the data as live and includes the matched location and observation time. The UI displays current conditions for the matched origin and destination after a plan is generated. This is not a route forecast or official travel advisory; verify warnings with official sources. If the provider fails, the endpoint returns an error instead of inventing a weather result.

## Saved plans and audit history

- `POST /api/recovery/saved-plans` — generate and save a proposal to local SQLite.
- `GET /api/recovery/saved-plans` — list saved proposals.
- `GET /api/recovery/saved-plans/{id}` — retrieve a proposal and its audit events.
- `POST /api/recovery/saved-plans/{id}/resolve` — record a feasible option chosen by the traveller. This records a decision only; it does not book transport.
- `GET /api/recovery/saved-plans/{id}/audit` — read the audit trail.

The browser interface also has controls to save a plan and review saved history. SQLite is stored in `strandedpool.db` by default; set `STRANDEDPOOL_DB` to choose another path.

## Health checks

- `GET /health` is a liveness check and confirms the process is responding.
- `GET /health/ready` checks that the configured SQLite database can be queried. It returns HTTP 503 if the database is unavailable.

Both endpoints explicitly identify the current data mode as simulated demo data.

## Tests

~~~bash
pytest -q
~~~

GitHub Actions runs the test suite on Python 3.11 and 3.12 for pushes to `main` and pull requests targeting `main`. The API contract tests cover readiness, invalid timestamps, timezone consistency, and missing saved plans.

## Project structure

~~~text
app/
  main.py            FastAPI API, deterministic recovery engine, browser UI
tests/
  test_engine.py     Health, UI, planning, and validation tests
requirements.txt
.env.example
~~~

## How ranking works

1. Generate a fixed set of explicitly simulated transport candidates.
2. Compute departure and arrival times from the submitted local departure time.
3. Check hard constraints: budget, arrival deadline, and enough demo seats for the group.
4. Sort feasible candidates before infeasible candidates, then use a transparent heuristic score to rank options.
5. Generate group-ride proposals and reminders for bookings that may be affected.

The score is a demo heuristic, not a probability or a guarantee. A recommendation marked feasible only satisfies the model's constraints against simulated inventory.

## Next steps for a production-grade system

- Integrate authorized, documented transport data providers and add forecast/official-alert sources for route-relevant weather.
- Track data provenance, freshness, and whether each result is live, cached, or simulated.
- Replace demo candidates with normalized provider results and handle API outages/rate limits.
- Add persistence, consent, authentication, and privacy controls for booking information.
- Add real group allocation optimization and capacity-safe reservation workflows.
- Add human confirmation before consequential booking or cancellation actions.
- Add deployment configuration, monitoring, accessibility checks, and provider-specific integration tests.

## Safety and privacy

Do not commit API keys, tokens, or real passenger booking data. Use .env locally and keep it out of version control. The app currently needs no secrets. Never rely on demo results for real travel decisions.

**Status:** runnable local MVP; live travel integrations and real bookings are not yet implemented.
