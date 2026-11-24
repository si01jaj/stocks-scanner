"""Tests de api_config: recursión con JSON corrupto y campo `enabled` ignorado."""
import json
import os

import pytest

import scripts.api_config as ac


@pytest.fixture
def config_aislada(tmp_path, monkeypatch):
    """Config y logs en un directorio temporal, sin tocar el repo."""
    destino = tmp_path / "config" / "api_keys.json"
    monkeypatch.setattr(ac, "DEFAULT_CONFIG_PATH", str(destino))
    monkeypatch.setattr(ac, "LOGS_DIR", str(tmp_path / "logs"))
    for var in ("FINNHUB_API_KEY", "ALPHA_VANTAGE_API_KEY", "POLYGON_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return destino


def test_config_corrupta_no_provoca_recursion(config_aislada):
    """
    Antes: load_config() → JSON inválido → init_config() → existe → load_config()
    → RecursionError.
    """
    config_aislada.parent.mkdir(parents=True, exist_ok=True)
    config_aislada.write_text("{ esto no es json valido ")

    config = ac.load_config()  # no debe lanzar RecursionError

    assert isinstance(config, dict)
    assert "finnhub" in config
    # se conserva el fichero roto por si tenía claves dentro
    respaldos = list(config_aislada.parent.glob("api_keys.json.corrupt-*"))
    assert len(respaldos) == 1
    # y el fichero nuevo es JSON válido
    assert json.loads(config_aislada.read_text())["finnhub"]["key"] is None


def test_config_valida_se_lee_sin_tocarla(config_aislada):
    config_aislada.parent.mkdir(parents=True, exist_ok=True)
    original = {"finnhub": {"key": "clave-real", "enabled": True}}
    config_aislada.write_text(json.dumps(original))

    assert ac.load_config() == original
    assert not list(config_aislada.parent.glob("*.corrupt-*"))


def test_enabled_false_implica_no_disponible():
    config = {"finnhub": {"key": "abcdef123456", "enabled": False}}
    assert ac.is_api_available("finnhub", config) is False


def test_enabled_true_con_clave_es_disponible():
    config = {"finnhub": {"key": "abcdef123456", "enabled": True}}
    assert ac.is_api_available("finnhub", config) is True


def test_sin_clave_no_es_disponible():
    config = {"finnhub": {"key": None, "enabled": True}}
    assert ac.is_api_available("finnhub", config) is False


def test_api_sin_clave_requerida_siempre_disponible():
    config = {"yfinance": {"key": None, "enabled": True}}
    assert ac.is_api_available("yfinance", config) is True


def test_api_gratuita_deshabilitada_tampoco_esta_disponible():
    config = {"yfinance": {"key": None, "enabled": False}}
    assert ac.is_api_available("yfinance", config) is False


def test_cada_api_devuelve_su_propia_clave(monkeypatch):
    """Regresión: el enriquecimiento cogía 'la primera clave que hubiera'."""
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    config = {
        "alpha_vantage": {"key": "CLAVE-DE-ALPHA-VANTAGE", "enabled": True},
        "finnhub": {"key": None, "enabled": True},
    }
    assert ac.get_api_key("finnhub", config) is None
    assert ac.get_api_key("alpha_vantage", config) == "CLAVE-DE-ALPHA-VANTAGE"
