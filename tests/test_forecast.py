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
            return FakeResponse({"results": [{
                "name": "Mysuru", "country": "India",
                "latitude": 12.2958, "longitude": 76.6393,
            }]})
        return FakeResponse({"daily": {
            "time": ["2026-10-21", "2026-10-22", "2026-10-23"],
            "weather_code": [1, 61, 3],
            "temperature_2m_max": [29.0, 27.5, 26.0],
            "temperature_2m_min": [19.0, 20.0, 18.5],
            "precipitation_probability_max": [10, 70, 20],
            "precipitation_sum": [0.0, 4.2, 0.1],
            "wind_speed_10m_max": [12.0, 18.0, 10.0],
        }})


def test_weather_forecast_returns_daily_live_provider_contract(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    response = client.get("/api/weather/forecast", params={"city": "Mysuru", "days": 3})

    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "LIVE WEATHER FORECAST"
    assert data["provider"] == "Open-Meteo"
    assert data["location"]["name"] == "Mysuru"
    assert len(data["forecast_days"]) == 3
    assert data["forecast_days"][1]["condition"] == "Slight rain"
    assert data["forecast_days"][1]["precipitation_probability_max"] == 70
    assert "not an official" in data["note"]


def test_weather_forecast_rejects_invalid_day_range():
    for days in (0, 8):
        response = client.get("/api/weather/forecast", params={"city": "Mysuru", "days": days})
        assert response.status_code == 422


def test_weather_forecast_returns_404_for_unknown_city(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    response = client.get("/api/weather/forecast", params={"city": "UnknownPlace"})

    assert response.status_code == 404
    assert "No location found" in response.json()["detail"]


def test_weather_forecast_rejects_blank_city():
    response = client.get("/api/weather/forecast", params={"city": "   "})

    assert response.status_code == 422
