from fastapi import FastAPI
from pydantic import BaseModel, Field
import pandas as pd
import joblib

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