"""Tests de la API FastAPI: jobs en segundo plano, lock y token."""
import pytest
from fastapi.testclient import TestClient

import api.main as main

client = TestClient(main.app)


def test_status_responde():
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_history_expone_el_ledger():
    r = client.get("/api/history")
    assert r.status_code == 200
    cuerpo = r.json()
    assert "diagnostico" in cuerpo
    assert "stats" in cuerpo


def test_reports_responde():
    assert client.get("/api/reports").status_code == 200


def test_run_exige_token_si_esta_configurado(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secreto")
    assert client.post("/api/run").status_code == 401
    assert client.post("/api/run", headers={"x-api-token": "malo"}).status_code == 401


def test_run_devuelve_409_si_ya_hay_un_scan(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "")
    assert main._scan_lock.acquire(blocking=False)
    try:
        r = client.post("/api/run")
        assert r.status_code == 409
        assert r.json()["status"] == "already_running"
    finally:
        main._scan_lock.release()


def test_job_desconocido_es_404():
    assert client.get("/api/run/no-existe").status_code == 404
