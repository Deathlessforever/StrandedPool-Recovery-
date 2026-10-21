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


class FakeAsyncClient:
    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params):
        if "geocoding-api.open-meteo.com" in url:
            if params["name"].lower() == "unknownplace":
                return FakeResponse({"results": []})
            return FakeResponse({
                "results": [{
                    "name": "Mysuru",
                    "country": "India",
                    "latitude": 12.2958,
                    "longitude": 76.6393,
                }]
            })
        return FakeResponse({
            "current": {
                "time": "2026-10-21T12:00",
                "temperature_2m": 28.4,
                "apparent_temperature": 30.1,
                "relative_humidity_2m": 54,
                "precipitation": 0.0,
                "wind_speed_10m": 9.2,
                "weather_code": 1,
                "is_day": 1,
            }
        })


def test_current_weather_uses_live_provider_contract(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    response = client.get("/api/weather/current", params={"city": "Mysuru"})

    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "LIVE WEATHER DATA"
    assert data["provider"] == "Open-Meteo"
    assert data["location"]["name"] == "Mysuru"
    assert data["current"]["condition"] == "Mainly clear"
    assert data["current"]["temperature_c"] == 28.4
    assert "not a route forecast" in data["note"]


def test_weather_returns_404_for_unmatched_city(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    response = client.get("/api/weather/current", params={"city": "UnknownPlace"})

    assert response.status_code == 404
    assert "No location found" in response.json()["detail"]


def test_weather_rejects_blank_city():
    response = client.get("/api/weather/current", params={"city": "   "})

    assert response.status_code == 422
