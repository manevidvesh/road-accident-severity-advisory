import hashlib
import json
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

RECORDED_HASHES = {
    "final_severity_model.pkl": "d6a25db03abd373fb47e2645f6b8df6ae0dcac786d4d6cca19ae549c94f69426",
    "model_columns.pkl": "7e25dd44cdc7fdde1ed79137b639974c71d3619662836ac129d8b4e3cf847fd6",
    "fill_values.pkl": "00f1c69601985efa2f95d1527c4e40dee5a11125937eb75023ae822f79db087b"
}

RECORDED_BASELINE = {
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

results = {}

print("=" * 80)
print("STARTING 10-POINT BACKEND VERIFICATION SUITE")
print("=" * 80)

# TEST 1: /auto-populate daytime vs nighttime
print("\n[TEST 1] /auto-populate for daytime (12:00) and nighttime (22:00)...")
res_day = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T12:00:00"})
res_night = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T22:00:00"})
day_json = res_day.json()
night_json = res_night.json()

t1_pass = (
    res_day.status_code == 200 and
    res_night.status_code == 200 and
    day_json["prefilled_incident_report"]["sunrise_sunset"] == "Day" and
    day_json["prefilled_incident_report"]["hour"] == 12 and
    night_json["prefilled_incident_report"]["sunrise_sunset"] == "Night" and
    night_json["prefilled_incident_report"]["hour"] == 22
)
print(f"  Daytime status: {day_json['prefilled_incident_report']['sunrise_sunset']}, Hour: {day_json['prefilled_incident_report']['hour']}")
print(f"  Nighttime status: {night_json['prefilled_incident_report']['sunrise_sunset']}, Hour: {night_json['prefilled_incident_report']['hour']}")
results["Test 1: Daytime vs Nighttime Solar Status"] = "PASSED" if t1_pass else "FAILED"

# TEST 2: Timestamps with Z, explicit offsets, and naive local time
print("\n[TEST 2] Timestamps with Z, explicit offset (-07:00), and naive local time...")
res_z = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T19:00:00Z"})
res_off = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T12:00:00-07:00"})
res_naive = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T12:00:00"})

z_json = res_z.json()
off_json = res_off.json()
naive_json = res_naive.json()

t2_pass = (
    z_json["prefilled_incident_report"]["hour"] == 12 and z_json["prefilled_incident_report"]["sunrise_sunset"] == "Day" and
    off_json["prefilled_incident_report"]["hour"] == 12 and off_json["prefilled_incident_report"]["sunrise_sunset"] == "Day" and
    naive_json["prefilled_incident_report"]["hour"] == 12 and naive_json["prefilled_incident_report"]["sunrise_sunset"] == "Day"
)
print(f"  19:00Z converted to local hour: {z_json['prefilled_incident_report']['hour']} (Resolved: {z_json['provenance']['resolved_timestamp']})")
print(f"  12:00-07:00 local hour: {off_json['prefilled_incident_report']['hour']} (Resolved: {off_json['provenance']['resolved_timestamp']})")
print(f"  12:00 naive local hour: {naive_json['prefilled_incident_report']['hour']} (Resolved: {naive_json['provenance']['resolved_timestamp']})")
results["Test 2: Timezone and Offset Handling"] = "PASSED" if t2_pass else "FAILED"

# TEST 3: Offline/missing solar fallback
print("\n[TEST 3] Offline API / missing solar fallback (verify clock-hour fallback)...")
with patch("urllib.request.urlopen", side_effect=Exception("Simulated Network Outage")):
    res_off_day = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T12:00:00"})
    res_off_night = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T22:00:00"})

off_day_json = res_off_day.json()
off_night_json = res_off_night.json()

t3_pass = (
    res_off_day.status_code == 200 and
    res_off_night.status_code == 200 and
    off_day_json["prefilled_incident_report"]["sunrise_sunset"] == "Day" and
    off_night_json["prefilled_incident_report"]["sunrise_sunset"] == "Night" and
    "Fallback" in off_day_json["provenance"]["sources"]["environmental_metrics"]
)
print(f"  Offline Day fallback: {off_day_json['prefilled_incident_report']['sunrise_sunset']} (Solar Source: {off_day_json['provenance']['sources']['solar_phase']})")
print(f"  Offline Night fallback: {off_night_json['prefilled_incident_report']['sunrise_sunset']} (Solar Source: {off_night_json['provenance']['sources']['solar_phase']})")
results["Test 3: Missing/Offline Solar Fallback"] = "PASSED" if t3_pass else "FAILED"

# TEST 4: Numerical input validation boundaries
print("\n[TEST 4] Numerical input validation boundaries...")
invalid_payloads = [
    ({"humidity": -10.0}, "humidity < 1.0"),
    ({"humidity": 150.0}, "humidity > 100.0"),
    ({"temperature": -100.0}, "temperature < -80.0"),
    ({"temperature": 160.0}, "temperature > 140.0"),
    ({"visibility": -2.0}, "visibility < 0.0"),
    ({"visibility": 60.0}, "visibility > 50.0"),
    ({"wind_speed": -5.0}, "wind_speed < 0.0"),
    ({"wind_speed": 200.0}, "wind_speed > 150.0"),
    ({"precipitation": -1.0}, "precipitation < 0.0"),
    ({"precipitation": 25.0}, "precipitation > 15.0"),
    ({"month": 0}, "month < 1"),
    ({"month": 13}, "month > 12"),
    ({"hour": -1}, "hour < 0"),
    ({"hour": 24}, "hour > 23"),
]

all_rejected = True
for payload, desc in invalid_payloads:
    res = client.post("/predict", json=payload)
    if res.status_code != 422:
        print(f"  FAILED to reject {desc}: status {res.status_code}")
        all_rejected = False

# Also test valid boundary values
valid_res = client.post("/predict", json={
    "temperature": 135.0,
    "humidity": 98.0,
    "visibility": 45.0,
    "wind_speed": 120.0,
    "precipitation": 12.0,
    "month": 12,
    "hour": 23
})
valid_accepted = valid_res.status_code == 200
print(f"  All 14 invalid boundary tests rejected with HTTP 422: {all_rejected}")
print(f"  Extreme but valid physical values accepted with HTTP 200: {valid_accepted}")
t4_pass = all_rejected and valid_accepted
results["Test 4: Input Validation & Boundary Constraints"] = "PASSED" if t4_pass else "FAILED"

# TEST 5: Omitted fields and explicit null values (median imputation)
print("\n[TEST 5] Omitted environmental fields and explicit null values...")
res_empty = client.post("/predict", json={})
res_nulls = client.post("/predict", json={
    "temperature": None,
    "humidity": None,
    "visibility": None,
    "wind_speed": None,
    "precipitation": None
})

t5_pass = (
    res_empty.status_code == 200 and
    res_nulls.status_code == 200 and
    res_empty.json()["predicted_severity_level"] in [1, 2, 3, 4] and
    res_nulls.json()["predicted_severity_level"] in [1, 2, 3, 4] and
    res_empty.json() == res_nulls.json()
)
print(f"  Empty payload status: {res_empty.status_code}, Severity: {res_empty.json().get('predicted_severity_level')}")
print(f"  Explicit nulls status: {res_nulls.status_code}, Severity: {res_nulls.json().get('predicted_severity_level')}")
print(f"  Empty and null-filled outputs match identically: {res_empty.json() == res_nulls.json()}")
results["Test 5: Omitted Fields & Explicit Null Imputation"] = "PASSED" if t5_pass else "FAILED"

# TEST 6: Unknown weather condition handling
print("\n[TEST 6] Unknown weather categories (reindexing resilience)...")
res_unknown = client.post("/predict", json={
    "weather_condition": "AlienSuperstorm",
    "sunrise_sunset": "TwilightZone"
})
t6_pass = (
    res_unknown.status_code == 200 and
    "predicted_severity_level" in res_unknown.json() and
    "confidence_distribution_percent" in res_unknown.json()
)
print(f"  Unknown category status: {res_unknown.status_code}, Severity: {res_unknown.json().get('predicted_severity_level')}")
results["Test 6: Unknown Categorical Value Resilience"] = "PASSED" if t6_pass else "FAILED"

# TEST 7: Comparison with recorded baseline prediction
print("\n[TEST 7] Comparison with recorded baseline prediction...")
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
res_baseline = client.post("/predict", json=baseline_payload)
baseline_pred = res_baseline.json()

t7_pass = (
    res_baseline.status_code == 200 and
    baseline_pred == RECORDED_BASELINE
)
print(f"  Recorded Baseline: {json.dumps(RECORDED_BASELINE, indent=2)}")
print(f"  Current Response:  {json.dumps(baseline_pred, indent=2)}")
print(f"  Identical Match:   {baseline_pred == RECORDED_BASELINE}")
results["Test 7: Baseline Prediction Equivalence"] = "PASSED" if t7_pass else "FAILED"

# TEST 8: Artifact SHA256 integrity check
print("\n[TEST 8] SHA256 hashes of model and preprocessing artifacts...")
hashes = {}
t8_pass = True
for fname, exp_hash in RECORDED_HASHES.items():
    with open(fname, "rb") as f:
        actual_hash = hashlib.sha256(f.read()).hexdigest()
    hashes[fname] = actual_hash
    match = (actual_hash == exp_hash)
    if not match:
        t8_pass = False
    print(f"  {fname}: {'MATCH' if match else 'MISMATCH'}")
    print(f"    Expected: {exp_hash}")
    print(f"    Actual:   {actual_hash}")

results["Test 8: Artifact SHA256 Hash Integrity"] = "PASSED" if t8_pass else "FAILED"

# TEST 9: CORS preflight verification
print("\n[TEST 9] CORS preflight OPTIONS request...")
res_cors = client.options("/predict", headers={
    "Origin": "http://localhost:3000",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type"
})
cors_allow_origin = res_cors.headers.get("access-control-allow-origin")
t9_pass = (
    res_cors.status_code == 200 and
    cors_allow_origin == "*"
)
print(f"  OPTIONS /predict status: {res_cors.status_code}")
print(f"  access-control-allow-origin header: {cors_allow_origin}")
print(f"  access-control-allow-methods: {res_cors.headers.get('access-control-allow-methods')}")
results["Test 9: CORS Preflight & Headers"] = "PASSED" if t9_pass else "FAILED"

# TEST 10: Complete E2E integration pipeline
print("\n[TEST 10] Complete integration tests across all endpoints...")
res_root = client.get("/")
res_health = client.get("/health")
res_auto = client.post("/auto-populate", json={"latitude": 39.7392, "longitude": -104.9903, "timestamp": "2026-11-21T12:00:00"})
prefilled = res_auto.json().get("prefilled_incident_report", {})
res_e2e_pred = client.post("/predict", json=prefilled)

t10_pass = (
    res_root.status_code == 200 and "Traffic Incident Advisory System" in res_root.text and
    res_health.status_code == 200 and res_health.json().get("status") == "operational" and
    res_auto.status_code == 200 and
    res_e2e_pred.status_code == 200 and
    "predicted_severity_level" in res_e2e_pred.json() and
    "dispatch_priority" in res_e2e_pred.json()
)
print(f"  GET /: {res_root.status_code} - {res_root.json().get('system')}")
print(f"  GET /health: {res_health.status_code} - {res_health.json()}")
print(f"  POST /auto-populate -> POST /predict pipeline: {res_e2e_pred.status_code}")
print(f"    Piped Result: Severity {res_e2e_pred.json().get('predicted_severity_level')} ({res_e2e_pred.json().get('impact_tier')}) - Priority: {res_e2e_pred.json().get('dispatch_priority')}")
results["Test 10: End-to-End Endpoint Integration Pipeline"] = "PASSED" if t10_pass else "FAILED"

print("\n" + "=" * 80)
print("FINAL SUMMARY OF TEST RESULTS:")
print("=" * 80)
all_tests_passed = True
for name, status in results.items():
    print(f"  [{status}] {name}")
    if status != "PASSED":
        all_tests_passed = False

print("\nOVERALL TEST SUITE VERDICT:", "ALL 10 TESTS PASSED (100%)" if all_tests_passed else "SOME TESTS FAILED")
