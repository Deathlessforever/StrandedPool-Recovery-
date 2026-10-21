from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    payload = {
        "origin": "Mysuru",
        "destination": "Mangaluru",
        "departure_time": "2026-10-21T09:00",
        "arrival_deadline": "2026-10-21T20:00",
        "budget_inr": 1500,
        "travellers": 2,
        "disruption": "cancelled",
        "include_pooling": True,
    }
    payload.update(overrides)
    return payload


def test_provider_status_does_not_overstate_live_integrations():
    response = client.get("/api/providers/status")

    assert response.status_code == 200
    data = response.json()
    assert data["transport_inventory"]["live_data_available"] is False
    assert data["current_weather"]["status"] == "implemented_on_demand"
    assert data["current_weather"]["connectivity"] == "not_checked_until_request"
    assert data["booking_actions"]["can_book"] is False
    assert data["booking_actions"]["can_cancel"] is False


def test_feasible_alternatives_are_ranked_before_infeasible_ones():
    response = client.post("/api/recovery/plan", json=base_payload())

    assert response.status_code == 200
    feasible = [option["feasible"] for option in response.json()["alternatives"]]
    assert feasible == sorted(feasible, reverse=True)


def test_pooling_can_be_disabled():
    response = client.post(
        "/api/recovery/plan",
        json=base_payload(include_pooling=False),
    )

    assert response.status_code == 200
    assert response.json()["pool_proposals"] == []


def test_same_origin_and_destination_are_rejected():
    response = client.post(
        "/api/recovery/plan",
        json=base_payload(destination=" mysuru "),
    )

    assert response.status_code == 422
    assert "different places" in response.json()["detail"]


def test_whitespace_only_origin_is_rejected():
    response = client.post(
        "/api/recovery/plan",
        json=base_payload(origin="   "),
    )

    assert response.status_code == 422
    assert "must not be blank" in response.json()["detail"]
