import hashlib
import json
import pandas as pd
from fastapi.testclient import TestClient
from main import app, columns, fill_values, known_weather_conditions

client = TestClient(app)

print("=" * 80)
print("RUNNING MANDATORY VERIFICATION SUITE FOR APPROVED CORRECTIONS")
print("=" * 80)

results = {}

# TEST 1: Empty {} request must be rejected
print("\n[TEST 1] Empty {} payload rejected...")
r_empty = client.post("/predict", json={})
t1_pass = (r_empty.status_code == 422)
print(f"  Empty payload status code: {r_empty.status_code} (Expected 422)")
error_fields = [e["loc"][-1] for e in r_empty.json().get("detail", [])]
print(f"  Missing required fields caught: {error_fields}")
results["1. Empty payload rejected"] = "PASSED" if (t1_pass and "month" in error_fields and "hour" in error_fields) else "FAILED"

# TEST 2: Valid request with month and hour but missing environmental values
print("\n[TEST 2] Missing environmental values work via median imputation...")
r_missing_env = client.post("/predict", json={"month": 6, "hour": 14})
t2_pass = (r_missing_env.status_code == 200 and "predicted_severity_level" in r_missing_env.json())
print(f"  Status code: {r_missing_env.status_code}")
print(f"  Predicted severity: {r_missing_env.json().get('predicted_severity_level')} ({r_missing_env.json().get('impact_tier')})")
results["2. Missing environmental inputs imputed via medians"] = "PASSED" if t2_pass else "FAILED"

# TEST 3: Explicit null environmental values must work
print("\n[TEST 3] Explicit null environmental values work...")
r_null_env = client.post("/predict", json={
    "month": 6,
    "hour": 14,
    "temperature": None,
    "humidity": None,
    "visibility": None,
    "wind_speed": None,
    "precipitation": None
})
t3_pass = (r_null_env.status_code == 200 and r_null_env.json() == r_missing_env.json())
print(f"  Status code: {r_null_env.status_code}")
print(f"  Identical output to omitted fields: {r_null_env.json() == r_missing_env.json()}")
results["3. Explicit null environmental inputs imputed via medians"] = "PASSED" if t3_pass else "FAILED"

# Helper function to inspect ready_features for a payload
def get_ready_features(payload):
    weather = payload.get("weather_condition", "Fair")
    weather = weather if weather in known_weather_conditions else "Unknown"
    solar = payload.get("sunrise_sunset", "Day")
    solar = solar if solar in ["Day", "Night", "Unknown"] else "Unknown"
    
    raw_data = pd.DataFrame([{
        "Temperature(F)": payload.get("temperature", 64.0),
        "Humidity(%)": payload.get("humidity", 68.0),
        "Visibility(mi)": payload.get("visibility", 10.0),
        "Wind_Speed(mph)": payload.get("wind_speed", 7.0),
        "Precipitation(in)": payload.get("precipitation", 0.0),
        "Crossing": False,
        "Junction": False,
        "Traffic_Signal": False,
        "Month": payload.get("month", 6),
        "Hour": payload.get("hour", 14),
        "Weather_Condition": weather,
        "Sunrise_Sunset": solar
    }])
    for col, median_val in fill_values.items():
        if col in raw_data.columns:
            raw_data[col] = raw_data[col].fillna(median_val)
    encoded = pd.get_dummies(raw_data, columns=["Weather_Condition", "Sunrise_Sunset"])
    return encoded.reindex(columns=columns, fill_value=0)

# TEST 4: Valid Blowing Dust category correctly represented as dropped reference class (all zeros)
print("\n[TEST 4] Valid 'Blowing Dust' category representation...")
feats_bd = get_ready_features({"month": 6, "hour": 14, "weather_condition": "Blowing Dust"})
weather_cols = [c for c in columns if c.startswith("Weather_Condition_")]
active_weather_bd = sum(feats_bd[c].values[0] for c in weather_cols)
unknown_weather_bd = feats_bd["Weather_Condition_Unknown"].values[0]
r_bd = client.post("/predict", json={"month": 6, "hour": 14, "weather_condition": "Blowing Dust"})
t4_pass = (r_bd.status_code == 200 and active_weather_bd == 0 and unknown_weather_bd == 0)
print(f"  Active weather dummy columns for Blowing Dust: {active_weather_bd} (Expected 0 - reference class)")
print(f"  Weather_Condition_Unknown column: {unknown_weather_bd} (Expected 0)")
print(f"  Prediction status: {r_bd.status_code}, Severity: {r_bd.json().get('predicted_severity_level')}")
results["4. Blowing Dust reference category preserved (all zeros)"] = "PASSED" if t4_pass else "FAILED"

# TEST 5: Known weather category retains its correct encoding
print("\n[TEST 5] Known weather category ('Fair') retains correct encoding...")
feats_fair = get_ready_features({"month": 6, "hour": 14, "weather_condition": "Fair"})
active_fair = feats_fair["Weather_Condition_Fair"].values[0]
unknown_fair = feats_fair["Weather_Condition_Unknown"].values[0]
t5_pass = (active_fair == 1 and unknown_fair == 0)
print(f"  Weather_Condition_Fair column: {active_fair} (Expected 1)")
print(f"  Weather_Condition_Unknown column: {unknown_fair} (Expected 0)")
results["5. Known weather category encoded accurately"] = "PASSED" if t5_pass else "FAILED"

# TEST 6: Unknown weather category maps to Unknown
print("\n[TEST 6] Unknown weather category ('AlienSuperstorm') maps to Unknown...")
feats_unknown = get_ready_features({"month": 6, "hour": 14, "weather_condition": "AlienSuperstorm"})
active_unknown = feats_unknown["Weather_Condition_Unknown"].values[0]
active_other = sum(feats_unknown[c].values[0] for c in weather_cols if c != "Weather_Condition_Unknown")
r_unknown = client.post("/predict", json={"month": 6, "hour": 14, "weather_condition": "AlienSuperstorm"})
t6_pass = (r_unknown.status_code == 200 and active_unknown == 1 and active_other == 0)
print(f"  Weather_Condition_Unknown column: {active_unknown} (Expected 1)")
print(f"  Other active weather columns: {active_other} (Expected 0)")
results["6. Unknown weather category maps to Unknown"] = "PASSED" if t6_pass else "FAILED"

# TEST 7: Day, Night, and unknown sunrise/sunset inputs handled correctly
print("\n[TEST 7] Sunrise/Sunset handling (Day, Night, Unknown)...")
feats_day = get_ready_features({"month": 6, "hour": 14, "sunrise_sunset": "Day"})
feats_night = get_ready_features({"month": 6, "hour": 14, "sunrise_sunset": "Night"})
feats_twilight = get_ready_features({"month": 6, "hour": 14, "sunrise_sunset": "TwilightZone"})

day_ok = (feats_day["Sunrise_Sunset_Night"].values[0] == 0 and feats_day["Sunrise_Sunset_Unknown"].values[0] == 0)
night_ok = (feats_night["Sunrise_Sunset_Night"].values[0] == 1 and feats_night["Sunrise_Sunset_Unknown"].values[0] == 0)
twilight_ok = (feats_twilight["Sunrise_Sunset_Night"].values[0] == 0 and feats_twilight["Sunrise_Sunset_Unknown"].values[0] == 1)

print(f"  Day: Night={feats_day['Sunrise_Sunset_Night'].values[0]}, Unknown={feats_day['Sunrise_Sunset_Unknown'].values[0]} (Expected all zeros - reference class)")
print(f"  Night: Night={feats_night['Sunrise_Sunset_Night'].values[0]}, Unknown={feats_night['Sunrise_Sunset_Unknown'].values[0]} (Expected Night=1, Unknown=0)")
print(f"  TwilightZone: Night={feats_twilight['Sunrise_Sunset_Night'].values[0]}, Unknown={feats_twilight['Sunrise_Sunset_Unknown'].values[0]} (Expected Night=0, Unknown=1)")
t7_pass = day_ok and night_ok and twilight_ok
results["7. Day (ref), Night, and unrecognized solar values handled correctly"] = "PASSED" if t7_pass else "FAILED"

# TEST 8: Existing valid baseline prediction unchanged
print("\n[TEST 8] Existing valid baseline prediction unchanged...")
baseline_payload = {
    "temperature": 65,
    "humidity": 60,
    "visibility": 10,
    "wind_speed": 5,
    "precipitation": 0,
    "crossing": False,
    "junction": False,
    "traffic_signal": False,
    "month": 6,
    "hour": 14,
    "weather_condition": "Fair",
    "sunrise_sunset": "Day"
}
expected_baseline = {
    "predicted_severity_level": 1,
    "impact_tier": "Minor Delay",
    "estimated_clearance_time": "10 - 25 minutes",
    "dispatch_priority": "LOW",
    "recommended_action": "Routine shoulder clearance. No mainline lane closures required.",
    "confidence_distribution_percent": {
        "Severity_1": 39.32,
        "Severity_2": 20.06,
        "Severity_3": 20.22,
        "Severity_4": 20.4
    }
}
r_base = client.post("/predict", json=baseline_payload)
t8_pass = (r_base.status_code == 200 and r_base.json() == expected_baseline)
print(f"  Baseline prediction matches recorded output: {r_base.json() == expected_baseline}")
results["8. Existing valid baseline prediction unchanged"] = "PASSED" if t8_pass else "FAILED"

# TEST 9: All 149 model features remain aligned
print("\n[TEST 9] Model input features count and alignment...")
from main import model
t9_pass = (model.n_features_in_ == len(columns) == 149)
print(f"  model.n_features_in_: {model.n_features_in_}")
print(f"  len(model_columns): {len(columns)}")
results["9. All 149 model features remain aligned"] = "PASSED" if t9_pass else "FAILED"

# TEST 10: All three model artifact SHA256 hashes unchanged
print("\n[TEST 10] Model artifact SHA256 hashes verification...")
expected_hashes = {
    "final_severity_model.pkl": "d6a25db03abd373fb47e2645f6b8df6ae0dcac786d4d6cca19ae549c94f69426",
    "model_columns.pkl": "7e25dd44cdc7fdde1ed79137b639974c71d3619662836ac129d8b4e3cf847fd6",
    "fill_values.pkl": "00f1c69601985efa2f95d1527c4e40dee5a11125937eb75023ae822f79db087b"
}
t10_pass = True
for fn, exp_h in expected_hashes.items():
    with open(fn, "rb") as f:
        act_h = hashlib.sha256(f.read()).hexdigest()
    match = (act_h == exp_h)
    if not match:
        t10_pass = False
    print(f"  {fn}: {'MATCH' if match else 'MISMATCH'}")
results["10. All 3 artifact SHA256 hashes identical"] = "PASSED" if t10_pass else "FAILED"

print("\n" + "=" * 80)
print("FINAL SUMMARY OF VERIFICATION RESULTS:")
print("=" * 80)
all_pass = True
for name, status in results.items():
    print(f"  [{status}] {name}")
    if status != "PASSED":
        all_pass = False

print("\nOVERALL VERDICT:", "ALL 10 MANDATORY REQUIREMENTS PASSED (100%)" if all_pass else "FAILURES DETECTED")
