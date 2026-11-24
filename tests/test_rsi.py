"""Tests del RSI único (scripts/technical_analysis._compute_rsi).

Regresión del bug histórico: la copia de run_screener.py devolvía 50 cuando
no había pérdidas en la ventana, es decir justo en una subida fuerte.
"""
import numpy as np
import pandas as pd

from scripts.technical_analysis import _compute_rsi


def test_subida_monotona_es_100():
    serie = pd.Series(np.arange(100.0, 140.0))
    assert _compute_rsi(serie, 14) == 100.0


def test_bajada_monotona_es_0():
    serie = pd.Series(np.arange(140.0, 100.0, -1))
    assert _compute_rsi(serie, 14) == 0.0


def test_serie_plana_no_devuelve_un_valor_inventado():
    serie = pd.Series([50.0] * 40)
    # 0/0 = NaN: se descarta el valor en vez de devolver 50 como si fuera real
    assert _compute_rsi(serie, 14) is None


def test_rango_valido_con_serie_mixta():
    rng = np.random.default_rng(42)
    serie = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    rsi = _compute_rsi(serie, 14)
    assert rsi is not None
    assert 0.0 <= rsi <= 100.0


def test_historia_insuficiente_no_revienta():
    serie = pd.Series([100.0, 101.0, 102.0])
    assert _compute_rsi(serie, 14) is None
