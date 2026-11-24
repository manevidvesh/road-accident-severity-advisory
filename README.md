# Road Accident Severity Advisory System

An operational decision-support system and Machine Learning API designed for **Traffic Operations Centers (TOC) & Incident Command (Department of Transportation)** to predict road accident traffic disruption severity (Levels 1 to 4) and provide real-time advisory protocols.

---

## 🚦 System Overview
Unlike basic models that only output raw class numbers, this system generates an **actionable incident advisory**:
* **Predicted Severity Level (1 to 4):** Measures traffic delay disruption.
* **Impact Tier:** Minor Delay, Moderate Disruption, Significant Backlog, or Critical Corridor Shutdown.
* **Estimated Clearance Time:** Operational recovery window (e.g. 45 - 90 minutes).
* **Dispatch Priority & Recommended Action:** Standard operating procedures (e.g. dispatching heavy recovery units, triggering Variable Message Signs).
* **Confidence Distribution:** Multi-class probabilities across all four severity tiers.

---

## 🧠 Model Architecture
* **Algorithm:** `RandomForestClassifier` with balanced class weights (`class_weight='balanced'`) to effectively catch rare severe traffic incidents despite extreme class imbalance.
* **Hyperparameters:** `n_estimators=100`, `max_depth=16`, `min_samples_leaf=50`.
* **Dataset:** US Accidents (2016 - 2023).
* **Input Features:** Ambient weather conditions (temperature, humidity, visibility, wind speed, precipitation, weather conditions), time variables (month, hour, day/night), and infrastructure flags (crossing, junction, traffic signal).

---

## 🚀 How to Run Locally

### 1. Install Dependencies
```bash
pip install fastapi uvicorn pandas scikit-learn joblib
```

### 2. Start the FastAPI Server
```bash
python -m uvicorn main:app --reload
```
* **Landing Page:** `http://127.0.0.1:8000/`
* **Assessment Workspace:** `http://127.0.0.1:8000/workspace`
* **Swagger Documentation:** `http://127.0.0.1:8000/docs`
* **Health Check:** `http://127.0.0.1:8000/health`

---

## 🌐 Public Tunneling (ngrok)
To expose the local API to a public HTTPS URL:
```bash
ngrok http 8000
```

---

## 📡 Sample API Request (Postman)

* **Method:** `POST`
* **URL:** `http://127.0.0.1:8000/predict` (or your public ngrok URL)
* **Headers:** `Content-Type: application/json`

### Request Body:
```json
{
  "temperature": 45.0,
  "humidity": 80.0,
  "visibility": 5.0,
  "wind_speed": 12.0,
  "precipitation": 0.05,
  "crossing": false,
  "junction": true,
  "traffic_signal": false,
  "month": 11,
  "hour": 18,
  "weather_condition": "Light Rain",
  "sunrise_sunset": "Night"
}
```

### Response:
```json
{
  "predicted_severity_level": 3,
  "impact_tier": "Significant Traffic Backlog",
  "estimated_clearance_time": "45 - 90 minutes",
  "dispatch_priority": "HIGH",
  "recommended_action": "Activate digital Variable Message Signs (VMS) on corridor. Reroute upcoming transit.",
  "confidence_distribution_percent": {
    "Severity_1": 6.51,
    "Severity_2": 26.43,
    "Severity_3": 34.9,
    "Severity_4": 32.16
  }
}
```
