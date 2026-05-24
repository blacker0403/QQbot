from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

import httpx

from bot_app.config import WeatherAlertConfig

logger = logging.getLogger(__name__)

HEAVY_RAIN_CODES = {65, 82, 95, 96, 99}


@dataclass(frozen=True)
class WeatherHour:
    time: datetime
    temperature_c: float | None = None
    precipitation_probability: int | None = None
    precipitation_mm: float | None = None
    weather_code: int | None = None


@dataclass(frozen=True)
class WeatherAlert:
    key: str
    message: str


class OpenMeteoWeatherClient:
    def __init__(self, config: WeatherAlertConfig) -> None:
        self.config = config

    async def fetch_hourly(self) -> list[WeatherHour]:
        params = {
            "latitude": self.config.latitude,
            "longitude": self.config.longitude,
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
            "timezone": "Asia/Shanghai",
            "forecast_days": 2,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            response.raise_for_status()
        return parse_open_meteo_hourly(response.json())


def parse_open_meteo_hourly(data: dict) -> list[WeatherHour]:
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temperatures = hourly.get("temperature_2m") or []
    probabilities = hourly.get("precipitation_probability") or []
    precipitations = hourly.get("precipitation") or []
    weather_codes = hourly.get("weather_code") or []

    result: list[WeatherHour] = []
    for index, time_text in enumerate(times):
        try:
            parsed_time = datetime.fromisoformat(time_text)
        except (TypeError, ValueError):
            continue
        result.append(
            WeatherHour(
                time=parsed_time,
                temperature_c=_value_at(temperatures, index),
                precipitation_probability=_int_value_at(probabilities, index),
                precipitation_mm=_value_at(precipitations, index),
                weather_code=_int_value_at(weather_codes, index),
            )
        )
    return result


def evaluate_weather_alerts(
    hours: list[WeatherHour],
    config: WeatherAlertConfig,
    now: datetime | None = None,
) -> list[WeatherAlert]:
    now = now or datetime.now()
    future_limit = now + timedelta(hours=24)
    future_hours = sorted(
        [hour for hour in hours if now <= hour.time <= future_limit],
        key=lambda item: item.time,
    )
    alerts: list[WeatherAlert] = []
    alerts.extend(_temperature_alerts(future_hours, config))
    alerts.extend(_heavy_rain_alerts(future_hours, config))
    alerts.extend(_rain_lead_alerts(future_hours, config, now))
    return alerts


def _temperature_alerts(hours: list[WeatherHour], config: WeatherAlertConfig) -> list[WeatherAlert]:
    window = config.temperature_change_window_hours
    strongest: dict[str, tuple[WeatherHour, WeatherHour, float]] = {}
    for start_index, start_hour in enumerate(hours):
        end_index = start_index + window
        if end_index >= len(hours):
            continue
        end_hour = hours[end_index]
        if start_hour.temperature_c is None or end_hour.temperature_c is None:
            continue
        delta = end_hour.temperature_c - start_hour.temperature_c
        if abs(delta) < config.temperature_change_threshold_c:
            continue
        kind = "rise" if delta > 0 else "drop"
        previous = strongest.get(kind)
        if previous is None or abs(delta) > abs(previous[2]):
            strongest[kind] = (start_hour, end_hour, delta)

    alerts: list[WeatherAlert] = []
    for kind in ("rise", "drop"):
        value = strongest.get(kind)
        if value is None:
            continue
        start_hour, end_hour, delta = value
        title = "急剧升温" if kind == "rise" else "急剧降温"
        alerts.append(
            WeatherAlert(
                key=f"weather:temp:{kind}:{end_hour.time:%Y%m%d%H}",
                message="\n".join(
                    [
                        f"【天气提醒】{config.location_name}未来24小时可能{title}",
                        (
                            f"预计 {start_hour.time:%m-%d %H:%M} 到 {end_hour.time:%H:%M}："
                            f"{start_hour.temperature_c:.1f}°C -> {end_hour.temperature_c:.1f}°C"
                            f"（{delta:+.1f}°C）"
                        ),
                    ]
                ),
            )
        )
    return alerts


def _heavy_rain_alerts(hours: list[WeatherHour], config: WeatherAlertConfig) -> list[WeatherAlert]:
    alerts: list[WeatherAlert] = []
    for hour in _onset_hours(hours, lambda item: _is_heavy_rain(item, config)):
        amount = hour.precipitation_mm if hour.precipitation_mm is not None else 0.0
        probability = hour.precipitation_probability
        probability_text = f"，降雨概率 {probability}%" if probability is not None else ""
        alerts.append(
            WeatherAlert(
                key=f"weather:heavy-rain:{hour.time:%Y%m%d%H}",
                message="\n".join(
                    [
                        f"【天气提醒】{config.location_name}未来24小时可能出现强降雨",
                        f"预计时间：{hour.time:%m-%d %H:%M}",
                        f"预计小时降水：{amount:.1f}mm{probability_text}",
                    ]
                ),
            )
        )
    return alerts


def _rain_lead_alerts(
    hours: list[WeatherHour],
    config: WeatherAlertConfig,
    now: datetime,
) -> list[WeatherAlert]:
    lead = timedelta(hours=config.rain_lead_hours)
    alerts: list[WeatherAlert] = []
    for hour in _onset_hours(
        hours,
        lambda item: _is_likely_rain(item, config) and not _is_heavy_rain(item, config),
    ):
        if hour.time.date() != now.date():
            continue
        until_rain = hour.time - now
        if until_rain < timedelta(0) or until_rain > lead:
            continue
        amount = hour.precipitation_mm if hour.precipitation_mm is not None else 0.0
        probability = hour.precipitation_probability if hour.precipitation_probability is not None else 0
        alerts.append(
            WeatherAlert(
                key=f"weather:rain-lead:{hour.time:%Y%m%d%H}",
                message="\n".join(
                    [
                        f"【天气提醒】{config.location_name}今天大概率会下雨",
                        f"预计开始：{hour.time:%H:%M}",
                        f"降雨概率：{probability}%，预计小时降水：{amount:.1f}mm",
                    ]
                ),
            )
        )
    return alerts


def _onset_hours(hours: list[WeatherHour], predicate) -> list[WeatherHour]:
    result: list[WeatherHour] = []
    previous_matched = False
    for hour in hours:
        matched = predicate(hour)
        if matched and not previous_matched:
            result.append(hour)
        previous_matched = matched
    return result


def _is_heavy_rain(hour: WeatherHour, config: WeatherAlertConfig) -> bool:
    if hour.weather_code in HEAVY_RAIN_CODES:
        return True
    return (hour.precipitation_mm or 0.0) >= config.heavy_rain_threshold_mm


def _is_likely_rain(hour: WeatherHour, config: WeatherAlertConfig) -> bool:
    probability = hour.precipitation_probability
    precipitation = hour.precipitation_mm or 0.0
    if probability is None:
        return False
    return (
        probability >= config.rain_probability_threshold
        and precipitation >= config.rain_precipitation_threshold_mm
    )


def _value_at(values: list, index: int) -> float | None:
    try:
        value = values[index]
    except IndexError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value_at(values: list, index: int) -> int | None:
    value = _value_at(values, index)
    return int(value) if value is not None else None
