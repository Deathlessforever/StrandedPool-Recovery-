from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import app.main as main
from app.main import app

client = TestClient(app)


def payload(**overrides):
    data = {
        "origin": "Mysuru",
        "destination": "Mangaluru",
        "departure_time": "2026-10-21T09:00",
        "arrival_deadline": "2026-10-21T18:00",
        "budget_inr": 1500,
        "travellers": 2,
        "disruption": "cancelled",
        "include_pooling": True,
    }
    data.update(overrides)
    return data


def test_readiness_reports_database_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "health" / "ready.db")
    main.init_db()

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["database"] == "reachable"
    assert response.json()["data_mode"] == "SIMULATED DEMO DATA"


def test_invalid_iso_datetime_returns_validation_error():
    response = client.post(
        "/api/recovery/plan",
        json=payload(departure_time="not-a-date"),
    )

    assert response.status_code == 422
    assert "Times must use local ISO format" in response.json()["detail"]


def test_mixed_naive_and_timezone_aware_times_are_rejected():
    response = client.post(
        "/api/recovery/plan",
        json=payload(arrival_deadline="2026-10-21T18:00+05:30"),
    )

    assert response.status_code == 422
    assert "timezone-aware timestamps for both fields" in response.json()["detail"]


def test_timezone_aware_times_can_be_compared_consistently():
    response = client.post(
        "/api/recovery/plan",
        json=payload(
            departure_time="2026-10-21T09:00+05:30",
            arrival_deadline="2026-10-21T18:00+05:30",
        ),
    )

    assert response.status_code == 200


def test_unknown_saved_plan_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "missing-plan.db")
    main.init_db()

    response = client.get("/api/recovery/saved-plans/999999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Saved recovery plan not found"
