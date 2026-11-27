import os
import tempfile

# Use an isolated SQLite database for verification before importing main
_tmp_db = os.path.join(tempfile.gettempdir(), "toc_verify_test.db")
if os.path.exists(_tmp_db):
    os.remove(_tmp_db)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db}"

from fastapi.testclient import TestClient
from db import engine
from main import app


def test_all():
    print("=== STARTING COMPREHENSIVE VERIFICATION ===")

    with TestClient(app) as client:
        # 1. Health endpoint (model + DB)
        res = client.get("/health")
        assert res.status_code == 200, f"Health check failed: {res.status_code}"
        health_data = res.json()
        assert health_data["status"] == "operational"
        assert health_data["database_connected"] is True
        print("PASS: /health endpoint is operational with database connected")

        # 2. Root returns API JSON even for browser Accept headers (frontend hosted on Vercel)
        res = client.get("/", headers={"Accept": "text/html,application/json"})
        assert res.status_code == 200, f"Root failed: {res.status_code}"
        root_json = res.json()
        assert "system" in root_json and root_json.get("documentation") == "/docs"
        assert client.get("/workspace").status_code == 404
        print("PASS: Backend root serves API JSON only (/workspace not served by backend)")

        # 3. Frontend files exist on disk for Vercel deployment
        for fpath in [
            "frontend/index.html",
            "frontend/workspace.html",
            "frontend/css/styles.css",
            "frontend/js/config.js",
            "frontend/js/model-options.js",
            "frontend/js/ops.js",
            "frontend/js/app.js",
        ]:
            assert os.path.exists(fpath), f"Missing frontend file: {fpath}"
        print("PASS: All frontend static files present for Vercel")

        # 5. /auto-populate and stateless /predict
        auto_res = client.post("/auto-populate", json={
            "latitude": 30.2672,
            "longitude": -97.7431,
            "timestamp": "2026-11-20T14:30:00",
        })
        assert auto_res.status_code == 200
        report = auto_res.json()["prefilled_incident_report"]

        pred_payload = {
            "temperature": report["temperature"],
            "humidity": report["humidity"],
            "visibility": report["visibility"],
            "wind_speed": report["wind_speed"],
            "precipitation": report["precipitation"],
            "crossing": False,
            "junction": True,
            "traffic_signal": False,
            "month": report["month"],
            "hour": report["hour"],
            "weather_condition": report["weather_condition"],
            "sunrise_sunset": report["sunrise_sunset"],
        }
        pred_res = client.post("/predict", json=pred_payload)
        assert pred_res.status_code == 200
        assert pred_res.json()["predicted_severity_level"] in [1, 2, 3, 4]
        print("PASS: /auto-populate and /predict execute cleanly")

        # 6. Auth: unauthenticated /api/incidents rejected, registration + RBAC enforced
        unauth = client.get("/api/incidents")
        assert unauth.status_code == 401, f"Expected 401, got {unauth.status_code}"

        sup_signup = client.post("/api/auth/register", json={
            "full_name": "Attempted Admin",
            "email": "hack@toc.gov",
            "password": "Password123",
            "role": "supervisor",
        })
        assert sup_signup.status_code == 403, "Public supervisor registration should be blocked"

        reg_res = client.post("/api/auth/register", json={
            "full_name": "Priya Sharma",
            "email": "priya.responder@toc.gov",
            "password": "Password123",
            "role": "responder",
        })
        assert reg_res.status_code == 201, f"Register failed: {reg_res.text}"
        resp_token = reg_res.json()["access_token"]
        resp_headers = {"Authorization": f"Bearer {resp_token}"}

        disp_login = client.post("/api/auth/login", json={
            "email": "dispatcher@toc.gov",
            "password": "TocDemo@2026",
        })
        assert disp_login.status_code == 200
        disp_headers = {"Authorization": f"Bearer {disp_login.json()['access_token']}"}

        sup_login = client.post("/api/auth/login", json={
            "email": "supervisor@toc.gov",
            "password": "TocDemo@2026",
        })
        assert sup_login.status_code == 200
        sup_headers = {"Authorization": f"Bearer {sup_login.json()['access_token']}"}
        print("PASS: Registration, login, and role protections verified")

        # 7. Full Incident CRUD + Re-assessment + Audit Trail + RBAC
        create_payload = dict(pred_payload, **{
            "location_label": "I-35 Corridor, Austin TX",
            "latitude": 30.2672,
            "longitude": -97.7431,
            "occurred_at": "2026-11-25T14:30:00",
            "telemetry_provenance": auto_res.json()["field_status"],
            "notes": "Two lanes blocked near exit.",
        })
        c_res = client.post("/api/incidents", json=create_payload, headers=resp_headers)
        assert c_res.status_code == 201, f"Create incident failed: {c_res.text}"
        created = c_res.json()
        inc_id = created["id"]
        assert created["reference_code"].startswith("INC-")
        assert created["status"] == "open"
        assert len(created["assessments"]) == 1
        assert len(created["audit_logs"]) == 1

        # Responder cannot change status
        forbid_status = client.patch(f"/api/incidents/{inc_id}", json={"status": "dispatched"}, headers=resp_headers)
        assert forbid_status.status_code == 403

        # Dispatcher updates status + junction + re-assesses
        u_res = client.patch(
            f"/api/incidents/{inc_id}",
            json={"status": "dispatched", "junction": False, "notes": "Patrol Unit 14 dispatched", "reassess": True},
            headers=disp_headers,
        )
        assert u_res.status_code == 200, f"Update failed: {u_res.text}"
        updated = u_res.json()
        assert updated["status"] == "dispatched"
        assert len(updated["assessments"]) == 2
        assert len(updated["audit_logs"]) == 2

        # List & Filter
        list_res = client.get("/api/incidents?status=dispatched&q=INC-", headers=disp_headers)
        assert list_res.status_code == 200
        assert any(i["id"] == inc_id for i in list_res.json())

        # Responder cannot delete once dispatched; Supervisor can delete
        forbid_del = client.delete(f"/api/incidents/{inc_id}", headers=resp_headers)
        assert forbid_del.status_code == 403

        # Analytics summary (dispatcher/supervisor allowed, responder forbidden)
        assert client.get("/api/analytics/summary", headers=resp_headers).status_code == 403
        ana_res = client.get("/api/analytics/summary", headers=sup_headers)
        assert ana_res.status_code == 200
        assert ana_res.json()["total_incidents"] >= 1

        # Supervisor user management
        users_res = client.get("/api/users", headers=sup_headers)
        assert users_res.status_code == 200
        assert len(users_res.json()) >= 4

        # Delete incident (Supervisor)
        del_res = client.delete(f"/api/incidents/{inc_id}", headers=sup_headers)
        assert del_res.status_code == 200 and del_res.json()["deleted"] is True
        assert client.get(f"/api/incidents/{inc_id}", headers=sup_headers).status_code == 404
        print("PASS: Full Incident CRUD, re-assessment, audit trail, RBAC, and analytics verified")

        # 8. Swagger /docs and /openapi.json
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        print("PASS: /docs and /openapi.json operational")

    engine.dispose()
    if os.path.exists(_tmp_db):
        os.remove(_tmp_db)

    print("\n=== ALL COMPREHENSIVE CHECKS PASSED SUCCESSFULLY ===")


if __name__ == "__main__":
    test_all()
