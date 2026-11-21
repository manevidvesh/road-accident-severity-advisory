from fastapi import FastAPI
from pydantic import BaseModel, Field
import pandas as pd
import joblib
from datetime import datetime, timezone
import json
import urllib.request
import urllib.error

app = FastAPI(
    title="Traffic Incident Command & Severity Advisory API",
    description="Operational Decision-Support System for Traffic Operations Centers (TOC)",
    version="1.0"
)

model = joblib.load("final_severity_model.pkl")
columns = joblib.load("model_columns.pkl")
fill_values = joblib.load("fill_values.pkl")


class IncidentReport(BaseModel):
    temperature: float = Field(64.0, description="Ambient Temperature in Fahrenheit")
    humidity: float = Field(68.0, description="Humidity Percentage (0-100)")
    visibility: float = Field(10.0, description="Visibility in Miles")
    wind_speed: float = Field(7.0, description="Wind Speed in MPH")
    precipitation: float = Field(0.0, description="Precipitation in Inches")
    crossing: bool = Field(False, description="Accident at Pedestrian Crossing")
    junction: bool = Field(False, description="Accident at Highway Junction/Interchange")
    traffic_signal: bool = Field(False, description="Accident at Traffic Signal")
    month: int = Field(11, ge=1, le=12, description="Month of incident (1-12)")
    hour: int = Field(18, ge=0, le=23, description="Hour of incident (0-23)")
    weather_condition: str = Field("Fair", description="Observed Weather (e.g. Fair, Rain, Fog)")
    sunrise_sunset: str = Field("Night", description="Time of day: Day or Night")


@app.get("/")
def root():
    return {
        "system": "Traffic Incident Advisory System",
        "stakeholder": "Department of Transportation / Traffic Operations Center",
        "documentation": "/docs"
    }


@app.get("/health")
def health_check():
    return {"status": "operational", "model_loaded": True}



@app.post("/predict")
def generate_incident_advisory(incident: IncidentReport):

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
        "Weather_Condition": incident.weather_condition,
        "Sunrise_Sunset": incident.sunrise_sunset
    }])

    
    for col, median_val in fill_values.items():
        if col in raw_data.columns:
            raw_data[col] = raw_data[col].fillna(median_val)


    raw_data["Weather_Condition"] = raw_data["Weather_Condition"].fillna("Unknown")
    raw_data["Sunrise_Sunset"] = raw_data["Sunrise_Sunset"].fillna("Unknown")


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
    - Sunrise/Sunset status (calculated from daily solar times)
    - Month and Hour (calculated from timestamp)
    - Infrastructure flags (defaults awaiting operator confirmation)
    """
    if query.timestamp:
        try:
            dt = datetime.fromisoformat(query.timestamp.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    month = dt.month
    hour = dt.hour

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={query.latitude}&longitude={query.longitude}&"
        f"hourly=temperature_2m,relative_humidity_2m,visibility,wind_speed_10m,precipitation,weather_code&"
        f"daily=sunrise,sunset&"
        f"temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch&"
        f"past_days=7&timezone=auto"
    )

    weather_data = None
    api_status = "live"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TrafficIncidentAdvisory/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                weather_data = json.loads(response.read().decode("utf-8"))
    except Exception:
        api_status = "offline"

    if weather_data and "hourly" in weather_data:
        hourly = weather_data["hourly"]
        times = hourly.get("time", [])

        target_prefix = dt.strftime("%Y-%m-%dT%H:00")
        target_idx = -1
        for idx, t in enumerate(times):
            if t.startswith(target_prefix):
                target_idx = idx
                break

        if target_idx == -1 and len(times) > 0:
            target_idx = len(times) - 1

        temp = float(hourly["temperature_2m"][target_idx]) if hourly["temperature_2m"][target_idx] is not None else fill_values.get("Temperature(F)", 64.0)
        humidity = float(hourly["relative_humidity_2m"][target_idx]) if hourly["relative_humidity_2m"][target_idx] is not None else fill_values.get("Humidity(%)", 68.0)

        raw_vis = hourly.get("visibility", [None])[target_idx]
        if raw_vis is not None:
            vis = round(min(float(raw_vis) / 1609.34, 10.0), 1)
        else:
            vis = fill_values.get("Visibility(mi)", 10.0)

        wind = float(hourly["wind_speed_10m"][target_idx]) if hourly["wind_speed_10m"][target_idx] is not None else fill_values.get("Wind_Speed(mph)", 7.0)
        precip = float(hourly["precipitation"][target_idx]) if hourly["precipitation"][target_idx] is not None else fill_values.get("Precipitation(in)", 0.0)

        wmo_code = hourly["weather_code"][target_idx] if hourly["weather_code"][target_idx] is not None else 1
        weather_cond = WMO_CODE_MAP.get(wmo_code, "Fair")

        sunrise_sunset = "Day"
        daily = weather_data.get("daily", {})
        if "sunrise" in daily and "sunset" in daily and len(daily["sunrise"]) > 0:
            sunrise_str = daily["sunrise"][0]
            sunset_str = daily["sunset"][0]
            current_iso = dt.strftime("%Y-%m-%dT%H:%M")
            if current_iso < sunrise_str or current_iso > sunset_str:
                sunrise_sunset = "Night"
            else:
                sunrise_sunset = "Day"
        else:
            sunrise_sunset = "Day" if 6 <= hour <= 18 else "Night"

        source_tag = "Open-Meteo Weather API"
    else:
        temp = fill_values.get("Temperature(F)", 64.0)
        humidity = fill_values.get("Humidity(%)", 68.0)
        vis = fill_values.get("Visibility(mi)", 10.0)
        wind = fill_values.get("Wind_Speed(mph)", 7.0)
        precip = fill_values.get("Precipitation(in)", 0.0)
        weather_cond = "Fair"
        sunrise_sunset = "Day" if 6 <= hour <= 18 else "Night"
        source_tag = "Fallback (System Training Median)"

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
        "resolved_timestamp": dt.isoformat(),
        "sources": {
            "environmental_metrics": source_tag,
            "weather_condition": f"{source_tag} (Mapped via WMO)",
            "solar_phase": "Calculated (Daily Solar Position)" if weather_data else "Calculated (Clock Hour)",
            "time_metrics": "Calculated (Incident Timestamp)",
            "road_infrastructure": "Default (Operator Review Required)"
        }
    }

    return {
        "status": "success",
        "prefilled_incident_report": populated_payload,
        "provenance": provenance_metadata
    }