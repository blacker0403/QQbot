from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import shutil
import uuid

import pytest

from bot_app.config import AppConfig
from bot_app.runtime import build_runtime
from bot_app.services.weather_alert import WeatherHour, evaluate_weather_alerts, parse_open_meteo_hourly


def _hours(start: datetime, temperatures: list[float]) -> list[WeatherHour]:
    return [
        WeatherHour(time=start + timedelta(hours=index), temperature_c=temperature)
        for index, temperature in enumerate(temperatures)
    ]


def test_weather_alert_config_defaults_to_jiulonghu() -> None:
    config = AppConfig(owner_qq="11111")

    assert config.weather_alert.enabled is True
    assert config.weather_alert.latitude == 31.887
    assert config.weather_alert.longitude == 118.822
    assert config.weather_alert.rain_lead_hours == 2


def test_temperature_spike_and_drop_generate_future_alerts() -> None:
    config = AppConfig(owner_qq="11111").weather_alert
    now = datetime(2026, 5, 21, 8, 0, 0)
    hours = _hours(now, [20, 21, 22, 27, 27, 26, 25, 18])

    alerts = evaluate_weather_alerts(hours, config, now=now)
    keys = {alert.key for alert in alerts}

    assert "weather:temp:rise:2026052111" in keys
    assert "weather:temp:drop:2026052115" in keys
    assert any("急剧升温" in alert.message for alert in alerts)
    assert any("急剧降温" in alert.message for alert in alerts)


def test_heavy_rain_generates_urgent_alert() -> None:
    config = AppConfig(owner_qq="11111").weather_alert
    now = datetime(2026, 5, 21, 8, 0, 0)
    hours = [
        WeatherHour(time=now + timedelta(hours=1), precipitation_probability=80, precipitation_mm=0.2),
        WeatherHour(time=now + timedelta(hours=2), precipitation_probability=95, precipitation_mm=9.0),
        WeatherHour(time=now + timedelta(hours=3), precipitation_probability=95, precipitation_mm=10.0),
    ]

    alerts = evaluate_weather_alerts(hours, config, now=now)

    assert [alert.key for alert in alerts if "heavy-rain" in alert.key] == [
        "weather:heavy-rain:2026052110"
    ]
    assert any("强降雨" in alert.message for alert in alerts)


def test_likely_rain_alerts_within_two_hour_lead_only_for_today() -> None:
    config = AppConfig(owner_qq="11111").weather_alert
    now = datetime(2026, 5, 21, 8, 15, 0)
    hours = [
        WeatherHour(time=datetime(2026, 5, 21, 9, 0), precipitation_probability=40, precipitation_mm=0.0),
        WeatherHour(time=datetime(2026, 5, 21, 10, 0), precipitation_probability=75, precipitation_mm=0.4),
        WeatherHour(time=datetime(2026, 5, 21, 11, 0), precipitation_probability=80, precipitation_mm=0.5),
        WeatherHour(time=datetime(2026, 5, 22, 9, 0), precipitation_probability=90, precipitation_mm=0.8),
    ]

    alerts = evaluate_weather_alerts(hours, config, now=now)

    assert [alert.key for alert in alerts if "rain-lead" in alert.key] == [
        "weather:rain-lead:2026052110"
    ]
    assert any("今天大概率会下雨" in alert.message for alert in alerts)


@pytest.mark.asyncio
async def test_weather_alert_keys_are_persisted_and_deduped() -> None:
    data_dir = Path("data") / f"test-weather-{uuid.uuid4().hex}"
    data_dir.mkdir(parents=True)
    runtime = build_runtime(
        AppConfig(
            owner_qq="11111",
            storage_path=str(data_dir / "state.json"),
        )
    )
    try:
        assert await runtime.store.claim_weather_alert_key("weather:rain-lead:2026052110") is True
        assert await runtime.store.claim_weather_alert_key("weather:rain-lead:2026052110") is False
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_parse_open_meteo_hourly_handles_missing_values() -> None:
    hours = parse_open_meteo_hourly(
        {
            "hourly": {
                "time": ["2026-05-21T10:00", "bad"],
                "temperature_2m": [25.5],
                "precipitation_probability": [80],
                "precipitation": [0.3],
                "weather_code": [61],
            }
        }
    )

    assert hours == [
        WeatherHour(
            time=datetime(2026, 5, 21, 10, 0),
            temperature_c=25.5,
            precipitation_probability=80,
            precipitation_mm=0.3,
            weather_code=61,
        )
    ]
