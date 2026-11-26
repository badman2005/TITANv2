import time
import types

import pytest

from app import demo
from app.main import DIST


# ---------------------------------------------------------------- bases / crates (feed the PWA)
def test_bases(client):
    b = client.get("/api/bases").json()
    assert [x["base_code"] for x in b] == ["ALPHA", "BRAVO", "CHARLIE"]
    assert b[0]["geofence_radius_m"] == 800


def test_crates_list_with_dispatch_and_filter(client):
    cs = client.get("/api/crates").json()
    assert len(cs) == 5
    c2 = next(c for c in cs if c["crate_id"] == 2)
    assert c2["movement_status"] == "IN_TRANSIT" and c2["item_count"] == 2
    assert c2["dispatch"]["vehicle_callsign"] == "TITAN-12" and c2["dispatch"]["status"] == "EN_ROUTE"
    assert c2["dispatch"]["origin_base_code"] == "ALPHA" and c2["dispatch"]["destination_base_code"] == "CHARLIE"
    transit = client.get("/api/crates", params={"movement_status": "IN_TRANSIT"}).json()
    assert [c["crate_id"] for c in transit] == [2]
    locked = [c for c in cs if c["is_locked_out"]]
    assert [c["crate_id"] for c in locked] == [4]


def test_crate_detail(client):
    c = client.get("/api/crates/3").json()
    assert c["base_code"] == "BRAVO" and c["dispatch"] is None and c["item_count"] == 2
    assert client.get("/api/crates/999").status_code == 404


# ---------------------------------------------------------------- the PWA is served by the API
@pytest.mark.skipif(DIST is None or not (DIST / "index.html").is_file(), reason="frontend not built")
class TestServesPwa:
    def test_index_and_client_routes(self, client):
        for path in ("/", "/inventory", "/some/deep/link"):
            r = client.get(path)
            assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "TITAN" in r.text

    def test_pwa_files(self, client):
        assert client.get("/manifest.webmanifest").json()["short_name"] == "TITAN"
        sw = client.get("/sw.js")
        assert sw.status_code == 200 and sw.headers["cache-control"] == "no-cache"
        assert client.get("/icons/icon-192.png").headers["content-type"] == "image/png"

    def test_api_errors_stay_json_and_docs_still_work(self, client):
        r = client.get("/api/does-not-exist")
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

    def test_no_path_traversal(self, client):
        r = client.get("/..%2f..%2fapp%2fconfig.py")
        assert "database_url" not in r.text

    def test_service_worker_never_caches_the_api(self, client):
        sw = client.get("/sw.js").text
        assert "/api" in sw and "NetworkFirst" not in sw and "StaleWhileRevalidate" not in sw


# ---------------------------------------------------------------- demo control
def test_demo_config(client):
    d = client.get("/api/demo/config").json()
    assert d["enabled"] and "offline_thermal" in d["scenarios"] and "ALPHA-BRAVO" in d["routes"]


def test_demo_validation(client):
    assert client.post("/api/demo/runs", json={"crate_id": 1, "route": "ALPHA-MARS"}).status_code == 422
    assert client.post("/api/demo/runs", json={"crate_id": 1, "scenario": "nope"}).status_code == 422
    assert client.post("/api/demo/runs", json={"crate_id": 1, "ticks": 5}).status_code == 422
    assert client.post("/api/demo/runs", json={"crate_id": 1, "route": "ALPHA-ALPHA"}).status_code == 422
    assert client.get("/api/demo/runs/nope").status_code == 404


def test_demo_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr(demo, "settings", types.SimpleNamespace(demo_enabled=False, self_url="http://127.0.0.1:9"))
    assert client.get("/api/demo/config").json()["enabled"] is False
    assert client.post("/api/demo/runs", json={"crate_id": 1}).status_code == 404


def test_demo_run_is_a_real_subprocess_with_captured_log(client, monkeypatch):
    # point the child at a dead port: it must fail fast with a readable message, proving the plumbing end to end
    monkeypatch.setattr(demo, "settings", types.SimpleNamespace(demo_enabled=True, self_url="http://127.0.0.1:9"))
    r = client.post("/api/demo/runs", json={"crate_id": 1, "scenario": "thermal", "ticks": 20, "delay": 0})
    assert r.status_code == 200
    run = r.json()
    assert run["crate_id"] == 1 and run["scenario"] == "thermal"
    for _ in range(100):
        s = client.get(f"/api/demo/runs/{run['id']}").json()
        if not s["running"]:
            break
        time.sleep(0.1)
    assert not s["running"] and s["returncode"] == 1
    assert any("Cannot reach the TITAN API" in line for line in s["lines"])
    assert client.get("/api/demo/runs").json()[0]["id"] == run["id"]
    # incremental polling
    assert client.get(f"/api/demo/runs/{run['id']}", params={"since": s["total_lines"]}).json()["lines"] == []


def test_demo_one_run_per_crate_and_stop(client, monkeypatch):
    monkeypatch.setattr(demo, "settings", types.SimpleNamespace(demo_enabled=True, self_url="http://127.0.0.1:9"))
    monkeypatch.setattr(demo.sys, "executable", demo.sys.executable)
    # long-lived child: python sleeping, via a patched argv builder
    import subprocess
    real_popen = subprocess.Popen
    monkeypatch.setattr(demo.subprocess, "Popen", lambda argv, **kw: real_popen(
        [demo.sys.executable, "-c", "import time; print('hi', flush=True); time.sleep(30)"], **kw))
    a = client.post("/api/demo/runs", json={"crate_id": 2}).json()
    assert client.post("/api/demo/runs", json={"crate_id": 2}).status_code == 409   # same crate already running
    stopped = client.post(f"/api/demo/runs/{a['id']}/stop").json()
    assert stopped["stopped"] is True
    for _ in range(50):
        if not client.get(f"/api/demo/runs/{a['id']}").json()["running"]:
            break
        time.sleep(0.1)
    assert not client.get(f"/api/demo/runs/{a['id']}").json()["running"]
