from fastapi.testclient import TestClient
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
    assert response.status_code == 500 or response.status_code == 422
