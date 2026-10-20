# StrandedPool — Predict. Pool. Recover.

A working, original MVP inspired by the problem and feature ideas described in [MAfshaan17/StrandedPool](https://github.com/MAfshaan17/StrandedPool): travel disruption recovery, ranked alternatives, shared-ride proposals, downstream booking checks, and an action pack.

This repository is an independent implementation. The original repository did not expose a LICENSE file when checked, so this project does not copy its source code. It implements the publicly described product concept in its own code.

## What works now

- Responsive browser UI for entering an affected journey.
- FastAPI backend with /health and POST /api/recovery/plan.
- Alternative ranking against budget, arrival deadline, and number of travellers.
- Simulated capacity checks and shared-ride pooling proposals.
- Downstream booking risk reminders for hotels and onward pickups.
- Copyable draft messages for hotel, pickup, and refund/assistance requests.
- Input validation, API documentation, and automated tests.

## Important data boundary

**All providers, times, fares, seats, and alternatives are simulated demo data.** This version does not fetch live rail/bus/flight inventory, weather, route times, or hotel availability. It does not book, cancel, send messages, or process refunds. Confirm details directly with the relevant provider before travelling.

The demo data exists to make the workflow usable and testable while authorized provider APIs and partnerships are evaluated. Live availability, payment, booking, and automated rebooking are not implemented.

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

## Tests

~~~bash
pytest -q
~~~

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

- Integrate authorized, documented transport and weather data providers.
- Track data provenance, freshness, and whether each result is live, cached, or simulated.
- Replace demo candidates with normalized provider results and handle API outages/rate limits.
- Add persistence, consent, authentication, and privacy controls for booking information.
- Add real group allocation optimization and capacity-safe reservation workflows.
- Add human confirmation before consequential booking or cancellation actions.
- Add deployment configuration, monitoring, accessibility checks, and provider-specific integration tests.

## Safety and privacy

Do not commit API keys, tokens, or real passenger booking data. Use .env locally and keep it out of version control. The app currently needs no secrets. Never rely on demo results for real travel decisions.

**Status:** runnable local MVP; live travel integrations and real bookings are not yet implemented.
