from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from pydantic import BaseModel

HttpGet = Callable[..., Any]


class LiveDataError(RuntimeError):
    """A safe, user-presentable structured live-data failure."""


class CurrencyQuote(BaseModel):
    amount: Decimal
    from_currency: str
    to_currency: str
    rate: Decimal
    converted_amount: Decimal
    reference_date: str
    provider: str = "Frankfurter"
    source_url: str = "https://frankfurter.dev/"


class WeatherReport(BaseModel):
    location: str
    country: str | None = None
    latitude: float
    longitude: float
    timezone: str
    observed_at: str
    temperature_c: Decimal
    apparent_temperature_c: Decimal | None = None
    condition: str
    weather_code: int
    wind_speed_kmh: Decimal | None = None
    provider: str = "Open-Meteo"
    source_url: str = "https://open-meteo.com/"


class WeatherForecast(BaseModel):
    location: str
    country: str | None = None
    latitude: float
    longitude: float
    timezone: str
    forecast_date: str
    temperature_max_c: Decimal
    temperature_min_c: Decimal
    condition: str
    weather_code: int
    precipitation_probability_max: Decimal | None = None
    provider: str = "Open-Meteo"
    source_url: str = "https://open-meteo.com/"


class LocalDateTimeResult(BaseModel):
    timezone: str
    locale: str
    instant: datetime
    answer: str
    response_kind: str = "local_datetime"
    used_web: bool = False


class FrankfurterClient:
    endpoint = "https://api.frankfurter.dev/v1/latest"

    def __init__(self, *, timeout_seconds: float = 8.0, http_get: HttpGet = requests.get) -> None:
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get

    def convert(
        self,
        amount: Decimal | str | int,
        from_currency: str,
        to_currency: str,
    ) -> CurrencyQuote:
        parsed_amount = self._amount(amount)
        base = self._currency(from_currency)
        quote = self._currency(to_currency)
        if base == quote:
            return CurrencyQuote(
                amount=parsed_amount,
                from_currency=base,
                to_currency=quote,
                rate=Decimal("1"),
                converted_amount=parsed_amount,
                reference_date=datetime.now(UTC).date().isoformat(),
            )
        try:
            response = self.http_get(
                self.endpoint,
                params={"base": base, "symbols": quote},
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            rate = Decimal(str(payload["rates"][quote]))
            reference_date = str(payload["date"])
        except (
            requests.RequestException,
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
        ) as exc:
            raise LiveDataError("Currency rates are temporarily unavailable.") from exc
        if rate <= 0:
            raise LiveDataError("The currency provider returned an invalid exchange rate.")
        return CurrencyQuote(
            amount=parsed_amount,
            from_currency=base,
            to_currency=quote,
            rate=rate,
            converted_amount=parsed_amount * rate,
            reference_date=reference_date,
        )

    @staticmethod
    def _currency(value: str) -> str:
        code = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError("Currency codes must contain exactly three letters.")
        return code

    @staticmethod
    def _amount(value: Decimal | str | int) -> Decimal:
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Amount must be a valid decimal number.") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("Amount must be a finite, non-negative number.")
        return amount


class OpenMeteoClient:
    geocoding_endpoint = "https://geocoding-api.open-meteo.com/v1/search"
    forecast_endpoint = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, *, timeout_seconds: float = 8.0, http_get: HttpGet = requests.get) -> None:
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get

    def current_weather(
        self,
        location: str,
        *,
        locale: str = "en",
        timezone: str = "auto",
    ) -> WeatherReport:
        cleaned_location = " ".join(location.split()).strip()
        if not cleaned_location or len(cleaned_location) > 100:
            raise ValueError("Location must contain between 1 and 100 characters.")
        language = locale.split("-", 1)[0].lower()
        if not re.fullmatch(r"[a-z]{2}", language):
            language = "en"
        try:
            geocode_response = self.http_get(
                self.geocoding_endpoint,
                params={
                    "name": cleaned_location,
                    "count": 1,
                    "language": language,
                    "format": "json",
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
            geocode_response.raise_for_status()
            geocode_payload = geocode_response.json()
            places = geocode_payload.get("results") or []
            if not places:
                raise LiveDataError(f"I could not find a location matching {cleaned_location}.")
            place = places[0]
            latitude = float(place["latitude"])
            longitude = float(place["longitude"])

            forecast_response = self.http_get(
                self.forecast_endpoint,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": ("temperature_2m,apparent_temperature,weather_code,wind_speed_10m"),
                    "temperature_unit": "celsius",
                    "wind_speed_unit": "kmh",
                    "timezone": timezone,
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
            forecast_response.raise_for_status()
            forecast_payload = forecast_response.json()
            current = forecast_payload["current"]
            code = int(current["weather_code"])
            return WeatherReport(
                location=str(place.get("name") or cleaned_location),
                country=place.get("country"),
                latitude=latitude,
                longitude=longitude,
                timezone=str(forecast_payload.get("timezone") or timezone),
                observed_at=str(current["time"]),
                temperature_c=Decimal(str(current["temperature_2m"])),
                apparent_temperature_c=self._optional_decimal(current.get("apparent_temperature")),
                condition=_weather_condition(code),
                weather_code=code,
                wind_speed_kmh=self._optional_decimal(current.get("wind_speed_10m")),
            )
        except LiveDataError:
            raise
        except (
            requests.RequestException,
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
        ) as exc:
            raise LiveDataError("Current weather is temporarily unavailable.") from exc

    def forecast_weather(
        self,
        location: str,
        *,
        day: str = "tomorrow",
        locale: str = "en",
        timezone: str = "auto",
    ) -> WeatherForecast:
        """Return a structured daily forecast instead of a current observation."""

        cleaned_location = " ".join(location.split()).strip()
        if not cleaned_location or len(cleaned_location) > 100:
            raise ValueError("Location must contain between 1 and 100 characters.")
        if day not in {"today", "tomorrow"}:
            raise ValueError("Weather forecasts support today or tomorrow.")
        language = locale.split("-", 1)[0].lower()
        if not re.fullmatch(r"[a-z]{2}", language):
            language = "en"
        try:
            geocode_response = self.http_get(
                self.geocoding_endpoint,
                params={
                    "name": cleaned_location,
                    "count": 1,
                    "language": language,
                    "format": "json",
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
            geocode_response.raise_for_status()
            places = geocode_response.json().get("results") or []
            if not places:
                raise LiveDataError(f"I could not find a location matching {cleaned_location}.")
            place = places[0]
            latitude = float(place["latitude"])
            longitude = float(place["longitude"])
            forecast_response = self.http_get(
                self.forecast_endpoint,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "daily": (
                        "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max"
                    ),
                    "temperature_unit": "celsius",
                    "timezone": timezone,
                    "forecast_days": 2,
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout_seconds,
            )
            forecast_response.raise_for_status()
            payload = forecast_response.json()
            daily = payload["daily"]
            index = 1 if day == "tomorrow" else 0
            code = int(daily["weather_code"][index])
            return WeatherForecast(
                location=str(place.get("name") or cleaned_location),
                country=place.get("country"),
                latitude=latitude,
                longitude=longitude,
                timezone=str(payload.get("timezone") or timezone),
                forecast_date=str(daily["time"][index]),
                temperature_max_c=Decimal(str(daily["temperature_2m_max"][index])),
                temperature_min_c=Decimal(str(daily["temperature_2m_min"][index])),
                condition=_weather_condition(code),
                weather_code=code,
                precipitation_probability_max=self._optional_decimal(
                    daily.get("precipitation_probability_max", [None, None])[index]
                ),
            )
        except LiveDataError:
            raise
        except (
            requests.RequestException,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
        ) as exc:
            raise LiveDataError("Weather forecast is temporarily unavailable.") from exc

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))


def resolve_timezone(
    browser_timezone: str | None,
    profile_timezone: str | None = None,
    fallback_timezone: str = "UTC",
) -> ZoneInfo:
    for candidate in (browser_timezone, profile_timezone, fallback_timezone, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def local_datetime_answer(
    query: str,
    *,
    browser_timezone: str | None = None,
    profile_timezone: str | None = None,
    fallback_timezone: str = "UTC",
    locale: str | None = None,
    now: datetime | None = None,
) -> LocalDateTimeResult:
    zone = resolve_timezone(browser_timezone, profile_timezone, fallback_timezone)
    instant = now.astimezone(zone) if now is not None else datetime.now(zone)
    normalized = query.lower()
    wants_time = bool(re.search(r"\btime\b", normalized))
    wants_date = bool(re.search(r"\b(date|day|today)\b", normalized)) or not wants_time
    if wants_time and wants_date:
        answer = f"It is {instant:%A, %B %-d, %Y at %-I:%M %p} ({zone.key})."
    elif wants_time:
        answer = f"It is {instant:%-I:%M %p} ({zone.key})."
    else:
        answer = f"Today is {instant:%A, %B %-d, %Y} ({zone.key})."
    return LocalDateTimeResult(
        timezone=zone.key,
        locale=locale or "en",
        instant=instant,
        answer=answer,
    )


def _weather_condition(code: int) -> str:
    conditions: dict[int, str] = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        56: "light freezing drizzle",
        57: "dense freezing drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        66: "light freezing rain",
        67: "heavy freezing rain",
        71: "slight snowfall",
        73: "moderate snowfall",
        75: "heavy snowfall",
        77: "snow grains",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        85: "slight snow showers",
        86: "heavy snow showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }
    return conditions.get(code, "unknown conditions")
