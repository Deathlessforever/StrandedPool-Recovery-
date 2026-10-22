from datetime import date
import os
from fastapi.testclient import TestClient
import app.flight_provider as flights
from app.main import app

client = TestClient(app)


def test_flight_status_never_claims_live_without_credentials(monkeypatch):
    monkeypatch.delenv("STRANDEDPOOL_DRCT_API_KEY", raising=False)
    response = client.get("/api/flights/status")
    assert response.status_code == 200
    assert response.json()["status"] in {"credentials_required", "sandbox_test_only"}
    assert response.json()["booking_enabled"] is False


def test_flight_search_without_credentials_fails_closed(monkeypatch):
    monkeypatch.delenv("STRANDEDPOOL_DRCT_API_KEY", raising=False)
    response = client.get(
        "/api/flights/search",
        params={
            "origin_airport": "BLR",
            "destination_airport": "DEL",
            "departure_date": "2026-12-15",
            "adults": 1,
        },
    )
    assert response.status_code == 503
    assert "No simulated flights" in response.json()["detail"]


def test_flight_search_rejects_same_airport():
    response = client.get(
        "/api/flights/search",
        params={
            "origin_airport": "BLR",
            "destination_airport": "BLR",
            "departure_date": "2026-12-15",
        },
    )
    assert response.status_code == 422


def test_provider_response_is_ranked_without_fabricating_missing_fields():
    payload = {
        "flights_options": [
            {"id": "more-expensive", "price": {"amount": 9200, "currency": "INR"},
             "duration": "5H 10M", "segments": ["s1", "s2"]},
            {"id": "cheaper", "price": {"amount": 6100, "currency": "INR"},
             "duration": "4H 25M", "segments": ["s3"]},
            {"id": "unknown", "segments": ["s4"]},
        ],
        "segments": [
            {"id": "s1"}, {"id": "s2"}, {"id": "s3"}, {"id": "s4"}
        ],
    }
    results = flights._normalise_options(payload)
    assert [item["provider_offer_id"] for item in results] == [
        "cheaper", "more-expensive", "unknown"
    ]
    assert results[0]["fare"] == 6100
    assert results[0]["duration_minutes"] == 265
    assert results[0]["stops"] == 0
    assert results[-1]["fare"] is None
    assert results[-1]["duration_minutes"] is None
    assert results[-1]["verification"] in {"PROVIDER_SEARCH_RESULT_UNPRICED", "SANDBOX_TEST_DATA"}
    assert results[-1]["simulated"] is (flights.DRCT_SANDBOX)


def test_provider_response_with_unrecognised_schema_is_not_replaced_by_demo():
    from fastapi import HTTPException
    try:
        flights._normalise_options({"data": []})
    except HTTPException as exc:
        assert exc.status_code == 502
        assert "no demo data was substituted" in exc.detail
    else:
        raise AssertionError("Unexpected provider schema should fail closed")



def test_normalises_documented_drct_offer_shape_and_transfer_time():
    payload = {
        "flights_options": [{
            "flights": [{"id": "F1", "journey_time": 19800, "segments": ["S1", "S2"]}],
            "offers": [{"id": "LH_test_offer", "price": {"amount": 182.4, "currency": "EUR"}}],
        }],
        "segments": [
            {"id": "S1", "transfer_time": 0, "duration": 9000, "departure_date": "2026-12-15", "departure_time": "06:30", "arrival_date": "2026-12-15", "arrival_time": "09:00"},
            {"id": "S2", "transfer_time": 1800, "duration": 9000, "departure_date": "2026-12-15", "departure_time": "09:30", "arrival_date": "2026-12-15", "arrival_time": "12:00"},
        ],
        "fares": [],
        "passengers": [{"id": "PAX1", "type": "ADT"}],
    }
    results = flights._normalise_options(payload)
    assert len(results) == 1
    item = results[0]
    assert item["provider_offer_id"] == "LH_test_offer"
    assert item["fare"] == 182.4
    assert item["currency"] == "EUR"
    assert item["duration_minutes"] == 330
    assert item["segments"] == 2
    assert item["stops"] == 1
    assert item["transfer_minutes"] == 30
    assert item["departure_local"] == "2026-12-15T06:30"
    assert item["arrival_local"] == "2026-12-15T12:00"
    assert item["availability_status"] == "provider_offer_returned_unrepriced"


def test_reprice_without_credentials_fails_closed(monkeypatch):
    monkeypatch.delenv("STRANDEDPOOL_DRCT_API_KEY", raising=False)
    response = client.post("/api/flights/reprice/LH_test_offer")
    assert response.status_code == 503
    assert "not revalidated" in response.json()["detail"]


def test_reprice_does_not_claim_seat_availability_from_price_only(monkeypatch):
    monkeypatch.setenv("STRANDEDPOOL_DRCT_API_KEY", "test-key")
    monkeypatch.setattr(flights, "DRCT_SANDBOX", False)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "LH_test_offer", "price": {"amount": 182.4, "currency": "EUR"}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def patch(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(flights.httpx, "AsyncClient", FakeAsyncClient)
    response = client.post("/api/flights/reprice/LH_test_offer")

    assert response.status_code == 200
    data = response.json()
    assert data["verification"] == "REPRICED_BY_PROVIDER"
    assert data["current_fare"] == 182.4
    assert data["availability_verified"] is False
    assert data["availability_status"] == "not_verified_by_price_revalidation"
    assert data["booking_completed"] is False
