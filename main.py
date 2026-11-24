import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import pandas as pd
import joblib
from datetime import datetime, timezone
import zoneinfo
import json
import urllib.request
import urllib.error

app = FastAPI(
    title="Traffic Incident Command & Severity Advisory API",
    description="Operational Decision-Support System for Traffic Operations Centers (TOC). Predicts traffic disruption severity levels (1 to 4) using a Random Forest classifier, and translates severity into standard DOT incident management clearance protocols.",
    version="1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Mount static asset folders for frontend
if os.path.exists("frontend"):
    if os.path.exists("frontend/css"):
        app.mount("/css", StaticFiles(directory="frontend/css"), name="css")
    if os.path.exists("frontend/js"):
        app.mount("/js", StaticFiles(directory="frontend/js"), name="js")
    if os.path.exists("frontend/assets"):
        app.mount("/assets", StaticFiles(directory="frontend/assets"), name="assets")
    app.mount("/frontend", StaticFiles(directory="frontend", html=True), name="frontend")

model = joblib.load("final_severity_model.pkl")
columns = joblib.load("model_columns.pkl")
fill_values = joblib.load("fill_values.pkl")

# Extract known weather categories from training columns, plus the dropped reference category ('Blowing Dust')
known_weather_conditions = {
    col.replace("Weather_Condition_", "") for col in columns if col.startswith("Weather_Condition_")
}
known_weather_conditions.add("Blowing Dust")


class IncidentReport(BaseModel):
    # Mandatory temporal context: an incident cannot occur without an observed time
    month: int = Field(..., ge=1, le=12, description="Month of incident (1-12, required)")
    hour: int = Field(..., ge=0, le=23, description="Hour of incident (0-23 in local solar time, required)")

    # Optional environmental telemetry: missing values default to None and are imputed with dataset medians
    temperature: float | None = Field(default=None, ge=-80.0, le=140.0, description="Ambient Temperature in Fahrenheit (-80 to 140°F, based on observed US extremes and sensor outlier filtering)")
    humidity: float | None = Field(default=None, ge=1.0, le=100.0, description="Relative Humidity Percentage (1 to 100%)")
    visibility: float | None = Field(default=None, ge=0.0, le=50.0, description="Surface Visibility in Statute Miles (0 to 50 mi, covers 99.98% of US METAR reports)")
    wind_speed: float | None = Field(default=None, ge=0.0, le=150.0, description="Sustained Wind Speed in MPH (0 to 150 mph)")
    precipitation: float | None = Field(default=None, ge=0.0, le=15.0, description="Precipitation accumulation in Inches (0 to 15 in)")

    # Infrastructure indicators: default to False indicating absence unless confirmed by dispatcher
    crossing: bool = Field(False, description="Accident at Pedestrian Crossing (default: False)")
    junction: bool = Field(False, description="Accident at Highway Junction/Interchange (default: False)")
    traffic_signal: bool = Field(False, description="Accident at Traffic Signal (default: False)")

    # Categorical context: unrecognized values are mapped to Unknown during preprocessing
    weather_condition: str = Field("Fair", description="Observed Weather (e.g. Fair, Rain, Snow, Blowing Dust; unrecognized categories mapped to Unknown)")
    sunrise_sunset: str = Field("Day", description="Solar Phase: Day or Night (unrecognized values mapped to Unknown)")


@app.get("/")
def root(request: Request):
    # If requested by a web browser, serve the product landing page
    if "text/html" in request.headers.get("accept", "") and os.path.exists("frontend/index.html"):
        return FileResponse("frontend/index.html")
    return {
        "system": "Traffic Incident Advisory System",
        "stakeholder": "Department of Transportation / Traffic Operations Center",
        "documentation": "/docs"
    }


@app.get("/workspace")
def workspace_route():
    if os.path.exists("frontend/workspace.html"):
        return FileResponse("frontend/workspace.html")
    return {"error": "Workspace page not found"}


@app.get("/index.html")
def index_html_route():
    if os.path.exists("frontend/index.html"):
        return FileResponse("frontend/index.html")
    return {"error": "Landing page not found"}


@app.get("/workspace.html")
def workspace_html_route():
    if os.path.exists("frontend/workspace.html"):
        return FileResponse("frontend/workspace.html")
    return {"error": "Workspace page not found"}


@app.get("/health")
def health_check():
    return {"status": "operational", "model_loaded": True}



@app.post("/predict")
def generate_incident_advisory(incident: IncidentReport):
    """
    Predict traffic incident disruption severity and return operational advisory protocols.

    NOTE ON ARCHITECTURE & METHODOLOGY:
    - The predicted severity level (1, 2, 3, or 4) and class confidence distribution are generated
      directly by the trained Random Forest classifier (`final_severity_model.pkl`).
    - The estimated clearance time windows, dispatch priorities, and recommended actions
      are deterministic DOT Standard Operating Procedure (SOP) policy mappings configured
      for each severity tier. They are NOT independent statistical/ML regression outputs.
    - Missing environmental metrics are imputed using precomputed dataset medians (`fill_values.pkl`).
    """
    # Map genuinely unrecognized categories to "Unknown", while preserving known categories
    # Note: 'Blowing Dust' is preserved as the training reference category (represented by all weather dummies = 0)
    weather = incident.weather_condition if incident.weather_condition in known_weather_conditions else "Unknown"
    solar = incident.sunrise_sunset if incident.sunrise_sunset in ["Day", "Night", "Unknown"] else "Unknown"

    # Step A: Convert incoming dispatcher data to DataFrame
    raw_data = pd.DataFrame([{
        "Temperature(F)": incident.temperature,
        "Humidity(%)": incident.humidity,
        "Visibility(mi)": incident.visibility,
        "Wind_Speed(mph)": incident.wind_speed,
        "Precipitation(in)": incident.precipitation,
        "Crossing": incident.crossing,
        "Junction": incident.junction,
        "Traffic_Signal": incident.traffic_signal,
        "Month": incident.month,
        "Hour": incident.hour,
        "Weather_Condition": weather,
        "Sunrise_Sunset": solar
    }])

    # Step B: Impute missing environmental values using training dataset medians
    for col, median_val in fill_values.items():
        if col in raw_data.columns:
            raw_data[col] = raw_data[col].fillna(median_val)

    # Step C: One-hot encode categorical features and align with 149 model columns
    encoded_data = pd.get_dummies(raw_data, columns=["Weather_Condition", "Sunrise_Sunset"])
    ready_features = encoded_data.reindex(columns=columns, fill_value=0)

    
    pred_severity = int(model.predict(ready_features)[0])
    probabilities = model.predict_proba(ready_features)[0]
    classes = list(model.classes_)

    
    advisory_protocols = {
        1: {
            "tier": "Minor Delay",
            "clearance_window": "10 - 25 minutes",
            "dispatch_priority": "LOW",
            "recommended_action": "Routine shoulder clearance. No mainline lane closures required."
        },
        2: {
            "tier": "Moderate Traffic Disruption",
            "clearance_window": "25 - 45 minutes",
            "dispatch_priority": "MEDIUM",
            "recommended_action": "Dispatch standard traffic patrol unit. Monitor corridor camera feeds."
        },
        3: {
            "tier": "Significant Traffic Backlog",
            "clearance_window": "45 - 90 minutes",
            "dispatch_priority": "HIGH",
            "recommended_action": "Activate digital Variable Message Signs (VMS) on corridor. Reroute upcoming transit."
        },
        4: {
            "tier": "Critical Corridor Shutdown",
            "clearance_window": "90+ minutes (Severe Gridlock)",
            "dispatch_priority": "CRITICAL",
            "recommended_action": "Dispatch heavy-duty recovery cranes & emergency teams. Trigger regional detour alerts."
        }
    }

    advisory = advisory_protocols.get(pred_severity, advisory_protocols[2])

    return {
        "predicted_severity_level": pred_severity,
        "impact_tier": advisory["tier"],
        "estimated_clearance_time": advisory["clearance_window"],
        "dispatch_priority": advisory["dispatch_priority"],
        "recommended_action": advisory["recommended_action"],
        "confidence_distribution_percent": {
            f"Severity_{c}": round(float(prob) * 100, 2)
            for c, prob in zip(classes, probabilities)
        }
    }


# =====================================================================
# AUTOMATIC INCIDENT CONTEXT POPULATION (OPEN-METEO INTEGRATION)
# =====================================================================

class LocationQuery(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Accident Latitude coordinate")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Accident Longitude coordinate")
    timestamp: str | None = Field(None, description="ISO timestamp (e.g. '2026-11-21T14:30:00'). Defaults to current time.")


WMO_CODE_MAP = {
    0: "Clear",
    1: "Fair",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Light Drizzle",
    53: "Drizzle",
    55: "Heavy Drizzle",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    80: "Light Rain",
    81: "Rain",
    82: "Heavy Rain",
    85: "Light Snow",
    86: "Snow",
    95: "Thunderstorm",
    96: "Thunderstorm with Hail",
    99: "Thunderstorm with Hail"
}


@app.post("/auto-populate")
def auto_populate_incident_data(query: LocationQuery):
    """
    Given (latitude, longitude, timestamp), automatically retrieves:
    - 5 Weather numerical metrics (Open-Meteo API in Imperial units)
    - Categorical Weather Condition (mapped from WMO code)
    - Sunrise/Sunset status (calculated from daily solar times matching the incident date)
    - Month and Hour (calculated from timestamp in coordinates' local timezone)
    - Infrastructure flags (defaults awaiting operator confirmation)

    Honesty rules (v1.1):
    - Weather is only returned if the incident hour is present in the Open-Meteo response
      (past 7 days up to the API's forecast horizon). Older incidents are NOT given the
      nearest/latest hour; their values are reported as imputed/unavailable instead.
    - `field_status` reports, per field, whether the value was "retrieved", "imputed"
      (replaced by a training-set median) or "unavailable" (categorical -> "Unknown").
    """
    if query.timestamp:
        try:
            dt = datetime.fromisoformat(query.timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="timestamp must be an ISO 8601 date-time, e.g. 2026-11-21T14:30:00",
            )
    else:
        dt = datetime.now(timezone.utc)

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={query.latitude}&longitude={query.longitude}&"
        f"hourly=temperature_2m,relative_humidity_2m,visibility,wind_speed_10m,precipitation,weather_code&"
        f"daily=sunrise,sunset&"
        f"temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch&"
        f"past_days=7&timezone=auto"
    )

    weather_data = None

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TrafficIncidentAdvisory/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                weather_data = json.loads(response.read().decode("utf-8"))
    except Exception:
        weather_data = None

    # Resolve local timezone and local timestamp
    local_tz = None
    if weather_data and "timezone" in weather_data:
        try:
            local_tz = zoneinfo.ZoneInfo(weather_data["timezone"])
        except Exception:
            local_tz = None

    if local_tz is None:
        local_tz = dt.tzinfo if dt.tzinfo is not None else timezone.utc

    if dt.tzinfo is not None:
        local_dt = dt.astimezone(local_tz)
    else:
        local_dt = dt.replace(tzinfo=local_tz)

    month = local_dt.month
    hour = local_dt.hour

    field_status = {}
    messages = []

    # Locate the incident hour. There is deliberately NO fallback to another hour:
    # using the latest hour for an old incident would silently return the wrong weather.
    hourly = (weather_data or {}).get("hourly") or {}
    times = hourly.get("time", [])
    target_prefix = local_dt.strftime("%Y-%m-%dT%H:00")
    target_idx = -1
    for idx, t in enumerate(times):
        if t.startswith(target_prefix):
            target_idx = idx
            break
    hour_matched = target_idx != -1

    if weather_data is None:
        messages.append("The weather service could not be reached, so no environmental data was retrieved.")
    elif not hour_matched:
        messages.append(
            "The weather source has no data for the incident hour. It covers roughly the past 7 days "
            "up to its forecast horizon, so older or far-future incidents cannot be retrieved."
        )

    def _numeric(field: str, key: str, fallback_key: str, default_val: float, convert=None) -> float:
        arr = hourly.get(key, [])
        if hour_matched and 0 <= target_idx < len(arr) and arr[target_idx] is not None:
            field_status[field] = "retrieved"
            raw = float(arr[target_idx])
            return convert(raw) if convert else raw
        field_status[field] = "imputed"
        return float(fill_values.get(fallback_key, default_val))

    temp = _numeric("temperature", "temperature_2m", "Temperature(F)", 64.0)
    humidity = _numeric("humidity", "relative_humidity_2m", "Humidity(%)", 68.0)
    vis = _numeric("visibility", "visibility", "Visibility(mi)", 10.0,
                   convert=lambda m: round(min(m / 1609.34, 10.0), 1))
    wind = _numeric("wind_speed", "wind_speed_10m", "Wind_Speed(mph)", 7.0)
    precip = _numeric("precipitation", "precipitation", "Precipitation(in)", 0.0)

    # Weather condition: Unknown (not Fair) when it cannot be determined or mapped
    weather_cond = "Unknown"
    field_status["weather_condition"] = "unavailable"
    codes_arr = hourly.get("weather_code", [])
    if hour_matched and 0 <= target_idx < len(codes_arr) and codes_arr[target_idx] is not None:
        mapped = WMO_CODE_MAP.get(codes_arr[target_idx])
        if mapped:
            weather_cond = mapped
            field_status["weather_condition"] = "retrieved"
        else:
            messages.append(f"Weather code {codes_arr[target_idx]} has no mapping to a model category; reported as Unknown.")

    # Solar phase: only from the retrieved sunrise/sunset for the incident date; otherwise Unknown
    sunrise_sunset = "Unknown"
    field_status["sunrise_sunset"] = "unavailable"
    solar_source = "Unavailable (no sunrise/sunset data for the incident date)"
    daily = (weather_data or {}).get("daily") or {}
    daily_times = daily.get("time", [])
    daily_sunrises = daily.get("sunrise", [])
    daily_sunsets = daily.get("sunset", [])
    target_date = local_dt.strftime("%Y-%m-%d")
    if target_date in daily_times:
        day_idx = daily_times.index(target_date)
        if day_idx < len(daily_sunrises) and day_idx < len(daily_sunsets):
            sunrise_str = daily_sunrises[day_idx]
            sunset_str = daily_sunsets[day_idx]
            if sunrise_str and sunset_str:
                current_iso = local_dt.strftime("%Y-%m-%dT%H:%M")
                sunrise_sunset = "Day" if sunrise_str <= current_iso <= sunset_str else "Night"
                field_status["sunrise_sunset"] = "retrieved"
                solar_source = "Calculated (Daily Sunrise/Sunset at Location)"

    if weather_data is None:
        source_tag = "Unavailable (weather service unreachable)"
    elif hour_matched:
        source_tag = "Open-Meteo Weather API"
    else:
        source_tag = "Unavailable (incident hour outside weather data window)"

    populated_payload = {
        "temperature": temp,
        "humidity": humidity,
        "visibility": vis,
        "wind_speed": wind,
        "precipitation": precip,
        "crossing": False,
        "junction": False,
        "traffic_signal": False,
        "month": month,
        "hour": hour,
        "weather_condition": weather_cond,
        "sunrise_sunset": sunrise_sunset
    }

    provenance_metadata = {
        "coordinates": {"latitude": query.latitude, "longitude": query.longitude},
        "resolved_timestamp": local_dt.isoformat(),
        "resolved_timezone": str(local_tz),
        "sources": {
            "environmental_metrics": source_tag,
            "weather_condition": f"{source_tag} (Mapped via WMO)",
            "solar_phase": solar_source,
            "time_metrics": "Calculated (Incident Timestamp)",
            "road_infrastructure": "Default (Operator Review Required)"
        }
    }

    env_fields = ["temperature", "humidity", "visibility", "wind_speed", "precipitation",
                  "weather_condition", "sunrise_sunset"]
    retrieved_count = sum(1 for f in env_fields if field_status[f] == "retrieved")
    if retrieved_count == len(env_fields):
        overall = "success"
    elif retrieved_count == 0:
        overall = "unavailable"
    else:
        overall = "partial"

    return {
        "status": overall,
        "prefilled_incident_report": populated_payload,
        "field_status": field_status,
        "messages": messages,
        "provenance": provenance_metadata
    }