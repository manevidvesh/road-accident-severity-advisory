from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import pandas as pd
import joblib
from datetime import datetime, timezone
import zoneinfo
import json
import urllib.request
import urllib.error
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from db import Base, SessionLocal, engine, get_db
from models import (
    Incident,
    IncidentAuditLog,
    Role,
    SeverityAssessment,
    User,
    VALID_STATUSES,
)
from auth import (
    create_access_token,
    get_current_user,
    hash_password,
    require_roles,
    seed_initial_data,
    verify_password,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_initial_data(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="Traffic Incident Command & Severity Advisory API",
    description="Operational Decision-Support System for Traffic Operations Centers (TOC). Predicts traffic disruption severity levels (1 to 4) using a Random Forest classifier, persists incident lifecycles in a normalized relational database, and enforces role-based stakeholder workflows.",
    version="2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

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
def root():
    return {
        "system": "Traffic Incident Advisory System",
        "stakeholder": "Department of Transportation / Traffic Operations Center",
        "documentation": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "operational", "model_loaded": True, "database_connected": db_ok}


ADVISORY_PROTOCOLS = {
    1: {
        "tier": "Minor Delay",
        "clearance_window": "10 - 25 minutes",
        "dispatch_priority": "LOW",
        "recommended_action": "Routine shoulder clearance. No mainline lane closures required.",
    },
    2: {
        "tier": "Moderate Traffic Disruption",
        "clearance_window": "25 - 45 minutes",
        "dispatch_priority": "MEDIUM",
        "recommended_action": "Dispatch standard traffic patrol unit. Monitor corridor camera feeds.",
    },
    3: {
        "tier": "Significant Traffic Backlog",
        "clearance_window": "45 - 90 minutes",
        "dispatch_priority": "HIGH",
        "recommended_action": "Activate digital Variable Message Signs (VMS) on corridor. Reroute upcoming transit.",
    },
    4: {
        "tier": "Critical Corridor Shutdown",
        "clearance_window": "90+ minutes (Severe Gridlock)",
        "dispatch_priority": "CRITICAL",
        "recommended_action": "Dispatch heavy-duty recovery cranes & emergency teams. Trigger regional detour alerts.",
    },
}


def run_severity_model(incident: IncidentReport) -> dict:
    """Run preprocessing, median imputation, Random Forest inference, and SOP tier mapping."""
    weather = incident.weather_condition if incident.weather_condition in known_weather_conditions else "Unknown"
    solar = incident.sunrise_sunset if incident.sunrise_sunset in ["Day", "Night", "Unknown"] else "Unknown"

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
        "Sunrise_Sunset": solar,
    }])

    for col, median_val in fill_values.items():
        if col in raw_data.columns:
            raw_data[col] = raw_data[col].fillna(median_val)

    encoded_data = pd.get_dummies(raw_data, columns=["Weather_Condition", "Sunrise_Sunset"])
    ready_features = encoded_data.reindex(columns=columns, fill_value=0)

    pred_severity = int(model.predict(ready_features)[0])
    probabilities = model.predict_proba(ready_features)[0]
    classes = list(model.classes_)

    advisory = ADVISORY_PROTOCOLS.get(pred_severity, ADVISORY_PROTOCOLS[2])
    return {
        "predicted_severity_level": pred_severity,
        "impact_tier": advisory["tier"],
        "estimated_clearance_time": advisory["clearance_window"],
        "dispatch_priority": advisory["dispatch_priority"],
        "recommended_action": advisory["recommended_action"],
        "confidence_distribution_percent": {
            f"Severity_{c}": round(float(prob) * 100, 2)
            for c, prob in zip(classes, probabilities)
        },
    }


@app.post("/predict")
def generate_incident_advisory(incident: IncidentReport):
    """
    Stateless prediction endpoint for quick previews and API testing.
    Persisted operational assessments use POST /api/incidents.
    """
    return run_severity_model(incident)


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


# =====================================================================
# AUTHENTICATION & STAKEHOLDER ROLE MANAGEMENT
# =====================================================================

class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=120)
    email: str = Field(..., min_length=5, max_length=180, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field(default="responder", description="responder or dispatcher")


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=180)
    password: str = Field(..., min_length=1, max_length=128)


class UserUpdateRequest(BaseModel):
    role: str | None = Field(default=None, description="responder, dispatcher, or supervisor")
    is_active: bool | None = None


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.name if user.role else "responder",
        "role_description": user.role.description if user.role else "",
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register_user(payload: RegisterRequest, db: Session = Depends(get_db)):
    requested_role = payload.role.strip().lower()
    if requested_role == "supervisor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Supervisor accounts must be provisioned by an existing supervisor.",
        )
    if requested_role not in ("responder", "dispatcher"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Role must be 'responder' or 'dispatcher'.",
        )

    email_norm = payload.email.strip().lower()
    if db.query(User).filter(User.email == email_norm).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

    role_obj = db.query(Role).filter(Role.name == requested_role).first()
    if not role_obj:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Role catalog not initialized.")

    user = User(
        full_name=payload.full_name.strip(),
        email=email_norm,
        password_hash=hash_password(payload.password),
        role_id=role_obj.id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    user = db.query(User).options(joinedload(User.role)).filter(User.id == user.id).first()

    token = create_access_token(user.id, user.role.name)
    return {"access_token": token, "token_type": "bearer", "user": _serialize_user(user)}


@app.post("/api/auth/login")
def login_user(payload: LoginRequest, db: Session = Depends(get_db)):
    email_norm = payload.email.strip().lower()
    user = db.query(User).options(joinedload(User.role)).filter(User.email == email_norm).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated.")

    token = create_access_token(user.id, user.role.name)
    return {"access_token": token, "token_type": "bearer", "user": _serialize_user(user)}


@app.get("/api/auth/me")
def get_me(current_user: User = Depends(get_current_user)):
    return _serialize_user(current_user)


@app.get("/api/users/dispatchers")
def list_assignable_staff(
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    users = (
        db.query(User)
        .join(Role)
        .options(joinedload(User.role))
        .filter(User.is_active.is_(True), Role.name.in_(["dispatcher", "supervisor"]))
        .order_by(User.full_name.asc())
        .all()
    )
    return [_serialize_user(u) for u in users]


@app.get("/api/users")
def list_all_users(
    _supervisor: User = Depends(require_roles("supervisor")),
    db: Session = Depends(get_db),
):
    users = db.query(User).options(joinedload(User.role)).order_by(User.id.asc()).all()
    return [_serialize_user(u) for u in users]


@app.patch("/api/users/{user_id}")
def update_user_account(
    user_id: int,
    payload: UserUpdateRequest,
    supervisor: User = Depends(require_roles("supervisor")),
    db: Session = Depends(get_db),
):
    user = db.query(User).options(joinedload(User.role)).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if payload.role is not None:
        role_name = payload.role.strip().lower()
        role_obj = db.query(Role).filter(Role.name == role_name).first()
        if not role_obj:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid role name.")
        user.role_id = role_obj.id

    if payload.is_active is not None:
        if user.id == supervisor.id and not payload.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own account.")
        user.is_active = payload.is_active

    db.commit()
    db.refresh(user)
    user = db.query(User).options(joinedload(User.role)).filter(User.id == user.id).first()
    return _serialize_user(user)


# =====================================================================
# PERSISTED INCIDENT COMMAND QUEUE (CRUD + LIFECYCLE + ANALYTICS)
# =====================================================================

class IncidentCreateRequest(IncidentReport):
    location_label: str = Field(..., min_length=1, max_length=300)
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    occurred_at: str = Field(..., min_length=4, max_length=64)
    telemetry_provenance: dict[str, str] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=2000)


class IncidentUpdateRequest(BaseModel):
    status: str | None = None
    assigned_to_id: int | None = None
    clear_assignee: bool = False
    notes: str | None = Field(default=None, max_length=2000)
    crossing: bool | None = None
    junction: bool | None = None
    traffic_signal: bool | None = None
    temperature: float | None = Field(default=None, ge=-80.0, le=140.0)
    humidity: float | None = Field(default=None, ge=1.0, le=100.0)
    visibility: float | None = Field(default=None, ge=0.0, le=50.0)
    wind_speed: float | None = Field(default=None, ge=0.0, le=150.0)
    precipitation: float | None = Field(default=None, ge=0.0, le=15.0)
    weather_condition: str | None = None
    sunrise_sunset: str | None = None
    reassess: bool = False


def _serialize_assessment(a: SeverityAssessment) -> dict:
    return {
        "id": a.id,
        "predicted_severity_level": a.predicted_severity,
        "impact_tier": a.impact_tier,
        "estimated_clearance_time": a.estimated_clearance_time,
        "dispatch_priority": a.dispatch_priority,
        "recommended_action": a.recommended_action,
        "confidence_distribution_percent": json.loads(a.confidence_json),
        "assessed_by": a.assessed_by.full_name if a.assessed_by else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _serialize_audit(log: IncidentAuditLog) -> dict:
    return {
        "id": log.id,
        "action": log.action,
        "from_status": log.from_status,
        "to_status": log.to_status,
        "detail": log.detail,
        "actor_name": log.actor.full_name if log.actor else "System",
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


def _serialize_incident(inc: Incident, include_history: bool = False) -> dict:
    latest = inc.assessments[0] if inc.assessments else None
    try:
        provenance = json.loads(inc.telemetry_provenance or "{}")
    except Exception:
        provenance = {}

    data = {
        "id": inc.id,
        "reference_code": inc.reference_code,
        "status": inc.status,
        "location_label": inc.location_label,
        "latitude": inc.latitude,
        "longitude": inc.longitude,
        "occurred_at": inc.occurred_at,
        "month": inc.month,
        "hour": inc.hour,
        "temperature": inc.temperature,
        "humidity": inc.humidity,
        "visibility": inc.visibility,
        "wind_speed": inc.wind_speed,
        "precipitation": inc.precipitation,
        "crossing": inc.crossing,
        "junction": inc.junction,
        "traffic_signal": inc.traffic_signal,
        "weather_condition": inc.weather_condition,
        "sunrise_sunset": inc.sunrise_sunset,
        "telemetry_provenance": provenance,
        "notes": inc.notes,
        "reported_by": {
            "id": inc.reported_by.id,
            "full_name": inc.reported_by.full_name,
            "role": inc.reported_by.role.name if inc.reported_by.role else "responder",
        } if inc.reported_by else None,
        "assigned_to": {
            "id": inc.assigned_to.id,
            "full_name": inc.assigned_to.full_name,
            "role": inc.assigned_to.role.name if inc.assigned_to.role else "dispatcher",
        } if inc.assigned_to else None,
        "latest_assessment": _serialize_assessment(latest) if latest else None,
        "created_at": inc.created_at.isoformat() if inc.created_at else None,
        "updated_at": inc.updated_at.isoformat() if inc.updated_at else None,
    }
    if include_history:
        data["assessments"] = [_serialize_assessment(a) for a in inc.assessments]
        data["audit_logs"] = [_serialize_audit(l) for l in inc.audit_logs]
    return data


def _load_incident_or_404(db: Session, incident_id: int) -> Incident:
    inc = (
        db.query(Incident)
        .options(
            joinedload(Incident.reported_by).joinedload(User.role),
            joinedload(Incident.assigned_to).joinedload(User.role),
            joinedload(Incident.assessments).joinedload(SeverityAssessment.assessed_by),
            joinedload(Incident.audit_logs).joinedload(IncidentAuditLog.actor),
        )
        .filter(Incident.id == incident_id)
        .first()
    )
    if not inc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident record not found.")
    return inc


@app.post("/api/incidents", status_code=status.HTTP_201_CREATED)
def create_incident(
    payload: IncidentCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    prediction = run_severity_model(payload)
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")

    inc = Incident(
        reference_code=f"TMP-{date_tag}-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        reported_by_id=current_user.id,
        assigned_to_id=current_user.id if current_user.role and current_user.role.name in ("dispatcher", "supervisor") else None,
        location_label=payload.location_label.strip(),
        latitude=payload.latitude,
        longitude=payload.longitude,
        occurred_at=payload.occurred_at.strip(),
        month=payload.month,
        hour=payload.hour,
        temperature=payload.temperature,
        humidity=payload.humidity,
        visibility=payload.visibility,
        wind_speed=payload.wind_speed,
        precipitation=payload.precipitation,
        crossing=payload.crossing,
        junction=payload.junction,
        traffic_signal=payload.traffic_signal,
        weather_condition=payload.weather_condition,
        sunrise_sunset=payload.sunrise_sunset,
        telemetry_provenance=json.dumps(payload.telemetry_provenance),
        status="open",
        notes=payload.notes.strip(),
    )
    db.add(inc)
    db.flush()
    inc.reference_code = f"INC-{date_tag}-{inc.id:04d}"

    assessment = SeverityAssessment(
        incident_id=inc.id,
        assessed_by_id=current_user.id,
        predicted_severity=prediction["predicted_severity_level"],
        impact_tier=prediction["impact_tier"],
        estimated_clearance_time=prediction["estimated_clearance_time"],
        dispatch_priority=prediction["dispatch_priority"],
        recommended_action=prediction["recommended_action"],
        confidence_json=json.dumps(prediction["confidence_distribution_percent"]),
    )
    db.add(assessment)

    db.add(
        IncidentAuditLog(
            incident_id=inc.id,
            actor_id=current_user.id,
            action="CREATED",
            from_status=None,
            to_status="open",
            detail=f"Logged incident and ran severity assessment (Level {prediction['predicted_severity_level']} · {prediction['dispatch_priority']}).",
        )
    )
    db.commit()
    db.expire_all()
    return _serialize_incident(_load_incident_or_404(db, inc.id), include_history=True)


@app.get("/api/incidents")
def list_incidents(
    status_filter: str | None = Query(default=None, alias="status"),
    severity: int | None = Query(default=None, ge=1, le=4),
    q: str | None = Query(default=None, max_length=120),
    mine_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = (
        db.query(Incident)
        .options(
            joinedload(Incident.reported_by).joinedload(User.role),
            joinedload(Incident.assigned_to).joinedload(User.role),
            joinedload(Incident.assessments).joinedload(SeverityAssessment.assessed_by),
        )
        .order_by(Incident.created_at.desc(), Incident.id.desc())
    )
    if status_filter and status_filter in VALID_STATUSES:
        query = query.filter(Incident.status == status_filter)
    if mine_only:
        query = query.filter(Incident.reported_by_id == current_user.id)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.filter(
            (Incident.location_label.ilike(pattern)) | (Incident.reference_code.ilike(pattern))
        )

    incidents = query.all()
    serialized = [_serialize_incident(inc, include_history=False) for inc in incidents]
    if severity is not None:
        serialized = [
            item for item in serialized
            if item["latest_assessment"] and item["latest_assessment"]["predicted_severity_level"] == severity
        ]
    return serialized


@app.get("/api/incidents/{incident_id}")
def get_incident(
    incident_id: int,
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _serialize_incident(_load_incident_or_404(db, incident_id), include_history=True)


@app.patch("/api/incidents/{incident_id}")
def update_incident(
    incident_id: int,
    payload: IncidentUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inc = _load_incident_or_404(db, incident_id)
    role_name = current_user.role.name if current_user.role else "responder"

    if role_name == "responder":
        if inc.reported_by_id != current_user.id or inc.status != "open":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Responders can only edit notes or conditions on their own open incidents.",
            )
        if payload.status is not None or payload.assigned_to_id is not None or payload.clear_assignee:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only dispatchers or supervisors can update incident status or assignment.",
            )

    changes = []
    old_status = inc.status

    if payload.status is not None:
        next_status = payload.status.strip().lower()
        if next_status not in VALID_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Status must be one of: {', '.join(VALID_STATUSES)}.",
            )
        if next_status != inc.status:
            inc.status = next_status
            changes.append(f"Status changed from {old_status} to {next_status}")

    if payload.clear_assignee:
        if inc.assigned_to_id is not None:
            inc.assigned_to_id = None
            changes.append("Cleared assignee")
    elif payload.assigned_to_id is not None:
        assignee = db.query(User).filter(User.id == payload.assigned_to_id, User.is_active.is_(True)).first()
        if not assignee:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignee account not found.")
        if inc.assigned_to_id != assignee.id:
            inc.assigned_to_id = assignee.id
            changes.append(f"Assigned to {assignee.full_name}")

    if payload.notes is not None and payload.notes.strip() != inc.notes:
        inc.notes = payload.notes.strip()
        changes.append("Updated operational notes")

    condition_fields = (
        "crossing",
        "junction",
        "traffic_signal",
        "temperature",
        "humidity",
        "visibility",
        "wind_speed",
        "precipitation",
        "weather_condition",
        "sunrise_sunset",
    )
    condition_changed = False
    for field in condition_fields:
        val = getattr(payload, field)
        if val is not None and val != getattr(inc, field):
            setattr(inc, field, val)
            condition_changed = True

    if condition_changed:
        changes.append("Updated roadway/environmental conditions")

    if condition_changed or payload.reassess:
        report = IncidentReport(
            month=inc.month,
            hour=inc.hour,
            temperature=inc.temperature,
            humidity=inc.humidity,
            visibility=inc.visibility,
            wind_speed=inc.wind_speed,
            precipitation=inc.precipitation,
            crossing=inc.crossing,
            junction=inc.junction,
            traffic_signal=inc.traffic_signal,
            weather_condition=inc.weather_condition,
            sunrise_sunset=inc.sunrise_sunset,
        )
        prediction = run_severity_model(report)
        db.add(
            SeverityAssessment(
                incident_id=inc.id,
                assessed_by_id=current_user.id,
                predicted_severity=prediction["predicted_severity_level"],
                impact_tier=prediction["impact_tier"],
                estimated_clearance_time=prediction["estimated_clearance_time"],
                dispatch_priority=prediction["dispatch_priority"],
                recommended_action=prediction["recommended_action"],
                confidence_json=json.dumps(prediction["confidence_distribution_percent"]),
            )
        )
        changes.append(f"Re-assessed severity (Level {prediction['predicted_severity_level']} · {prediction['dispatch_priority']})")

    if changes:
        db.add(
            IncidentAuditLog(
                incident_id=inc.id,
                actor_id=current_user.id,
                action="STATUS_UPDATED" if old_status != inc.status else ("REASSESSED" if (condition_changed or payload.reassess) else "UPDATED"),
                from_status=old_status if old_status != inc.status else None,
                to_status=inc.status if old_status != inc.status else None,
                detail="; ".join(changes) + ".",
            )
        )

    db.commit()
    db.expire_all()
    return _serialize_incident(_load_incident_or_404(db, inc.id), include_history=True)


@app.delete("/api/incidents/{incident_id}")
def delete_incident(
    incident_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inc = db.query(Incident).filter(Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident record not found.")

    role_name = current_user.role.name if current_user.role else "responder"
    can_delete = role_name == "supervisor" or (inc.reported_by_id == current_user.id and inc.status == "open")
    if not can_delete:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only supervisors or the original reporter (while status is open) can delete an incident.",
        )

    ref = inc.reference_code
    db.delete(inc)
    db.commit()
    return {"deleted": True, "id": incident_id, "reference_code": ref}


@app.get("/api/analytics/summary")
def analytics_summary(
    _staff: User = Depends(require_roles("dispatcher", "supervisor")),
    db: Session = Depends(get_db),
):
    incidents = db.query(Incident).options(joinedload(Incident.assessments)).all()
    by_status = {s: 0 for s in VALID_STATUSES}
    by_severity = {"1": 0, "2": 0, "3": 0, "4": 0}

    for inc in incidents:
        by_status[inc.status] = by_status.get(inc.status, 0) + 1
        if inc.assessments:
            sev = str(inc.assessments[0].predicted_severity)
            by_severity[sev] = by_severity.get(sev, 0) + 1

    total_assessments = db.query(SeverityAssessment).count()
    active_users = db.query(User).filter(User.is_active.is_(True)).count()

    return {
        "total_incidents": len(incidents),
        "total_assessments": total_assessments,
        "active_users": active_users,
        "by_status": by_status,
        "by_severity": by_severity,
    }