from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import app.main as main
from app.main import app

client = TestClient(app)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeRoutingClient:
    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        if "geocoding-api.open-meteo.com" in url:
            city = params["name"]
            if city == "Unknown Place":
                return FakeResponse({"results": []})
            if city == "Mysuru":
                return FakeResponse({"results": [{
                    "name": "Mysuru", "country": "India",
                    "latitude": 12.2958, "longitude": 76.6393,
                }]})
            return FakeResponse({"results": [{
                "name": "Mangaluru", "country": "India",
                "latitude": 12.9141, "longitude": 74.8560,
            }]})
        return FakeResponse({
            "code": "Ok",
            "routes": [{"distance": 250000, "duration": 18000}],
        })


def test_driving_route_returns_real_provider_contract(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeRoutingClient)

    response = client.get(
        "/api/route/driving",
        params={"origin": "Mysuru", "destination": "Mangaluru"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "LIVE ROUTING ESTIMATE"
    assert data["provider"] == "OSRM public routing service"
    assert data["origin"]["name"] == "Mysuru"
    assert data["destination"]["name"] == "Mangaluru"
    assert data["distance_km"] == 250.0
    assert data["estimated_driving_minutes"] == 300
    assert "not a public-transport timetable" in data["note"]


def test_driving_route_reports_unmatched_place(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeRoutingClient)

    response = client.get(
        "/api/route/driving",
        params={"origin": "Unknown Place", "destination": "Mangaluru"},
    )

    assert response.status_code == 404
    assert "No location found" in response.json()["detail"]


def test_driving_route_rejects_same_city():
    response = client.get(
        "/api/route/driving",
        params={"origin": "Mysuru", "destination": " mysuru "},
    )

    assert response.status_code == 422
    assert "different places" in response.json()["detail"]


def test_driving_route_rejects_missing_city():
    response = client.get(
        "/api/route/driving",
        params={"origin": "Mysuru", "destination": "  "},
    )

    assert response.status_code == 422
