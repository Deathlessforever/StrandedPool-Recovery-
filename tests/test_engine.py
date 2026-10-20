from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import app.main as main
from app.main import app, build_plan, Booking

client = TestClient(app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True

def test_homepage_serves_app():
    response = client.get("/")
    assert response.status_code == 200
    assert "Build recovery plan" in response.text

def test_plan_labels_simulated_data_and_returns_actions():
    response = client.post("/api/recovery/plan", json={
        "origin":"Mysuru", "destination":"Mangaluru",
        "departure_time":"2026-10-21T09:00",
        "arrival_deadline":"2026-10-21T18:00",
        "budget_inr":1500, "travellers":2, "disruption":"cancelled",
        "include_pooling":True
    })
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "SIMULATED DEMO DATA"
    assert len(data["alternatives"]) > 0
    assert len(data["action_pack"]) == 3
    assert all(a["simulated"] for a in data["alternatives"])

def test_rejects_deadline_before_departure():
    response = client.post("/api/recovery/plan", json={
        "origin":"Mysuru", "destination":"Mangaluru",
        "departure_time":"2026-10-21T18:00",
        "arrival_deadline":"2026-10-21T09:00",
        "budget_inr":1500, "travellers":1, "disruption":"cancelled"
    })
    assert response.status_code == 422

def test_saved_plan_history_and_decision_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    main.init_db()
    payload = {
        "origin":"Mysuru", "destination":"Mangaluru",
        "departure_time":"2026-10-21T09:00",
        "arrival_deadline":"2026-10-21T18:00",
        "budget_inr":1500, "travellers":2, "disruption":"cancelled",
        "include_pooling":True
    }
    saved = client.post("/api/recovery/saved-plans", json=payload)
    assert saved.status_code == 200
    data = saved.json()
    plan_id = data["plan_id"]
    assert data["status"] == "proposed"
    assert data["plan"]["data_mode"] == "SIMULATED DEMO DATA"

    history = client.get("/api/recovery/saved-plans")
    assert history.status_code == 200
    assert any(item["id"] == plan_id for item in history.json()["items"])

    resolved = client.post(
        f"/api/recovery/saved-plans/{plan_id}/resolve",
        json={"selected_provider":"Demo Express","decision_note":"Best feasible demo choice"}
    )
    assert resolved.status_code == 200
    assert "No transport was booked" in resolved.json()["note"]

    detail = client.get(f"/api/recovery/saved-plans/{plan_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "decision_recorded"
    assert detail.json()["selected_provider"] == "Demo Express"
    assert len(detail.json()["audit_events"]) == 2

def test_cannot_record_infeasible_option(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test-infeasible.db")
    main.init_db()
    saved = client.post("/api/recovery/saved-plans", json={
        "origin":"Mysuru", "destination":"Mangaluru",
        "departure_time":"2026-10-21T09:00",
        "arrival_deadline":"2026-10-21T10:00",
        "budget_inr":100, "travellers":30, "disruption":"cancelled"
    })
    assert saved.status_code == 200
    response = client.post(
        f"/api/recovery/saved-plans/{saved.json()['plan_id']}/resolve",
        json={"selected_provider":"Demo Express","decision_note":"test"}
    )
    assert response.status_code == 422
