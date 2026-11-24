"""Tests de entry_exit: el módulo generaba precios imposibles.

Caso reproducido en la revisión: precio 100, ATR 50, score 5 →
entradas 75 / 37,5 / -12,5 y stop -62,5.
"""
import pytest

from scripts.entry_exit import compute_entry_exit, format_entry_exit


def test_caso_extremo_no_genera_precios_negativos():
    r = compute_entry_exit(current_price=100.0, technicals={"atr_14": 50.0}, score=5.0)

    assert all(v > 0 for v in r["entries"].values()), r["entries"]
    assert all(v > 0 for v in r["targets"].values()), r["targets"]
    assert r["stop_loss"] > 0
    assert r["atr_reliable"] is False
    assert any("fuera de rango" in w for w in r["warnings"])


def test_stop_no_supera_el_tope_de_riesgo():
    """Con ATR 10 sobre precio 100 el stop acababa en 67,50 (-32,5%)."""
    r = compute_entry_exit(current_price=100.0, technicals={"atr_14": 10.0}, score=5.0)
    assert r["stop_loss"] >= 100.0 * 0.80


def test_niveles_coherentes_y_ordenados():
    r = compute_entry_exit(current_price=100.0, technicals={"atr_14": 2.0}, score=5.0)
    e = r["entries"]
    t = r["targets"]

    assert e["aggressive"] > e["moderate"] > e["conservative"] > 0
    assert t["target_1"] < t["target_2"] < t["target_3"]
    assert r["stop_loss"] < e["conservative"]
    assert r["operable"] is True
    assert r["atr_reliable"] is True


def test_atr_ausente_usa_el_dos_por_ciento():
    r = compute_entry_exit(current_price=100.0, technicals={}, score=5.0)
    assert r["atr"] == pytest.approx(2.0)
    assert r["atr_reliable"] is False
    assert r["operable"] is True


def test_atr_negativo_o_cero_no_rompe():
    for atr in (0, -5, None, "no-numero"):
        r = compute_entry_exit(current_price=100.0, technicals={"atr_14": atr}, score=5.0)
        assert all(v > 0 for v in r["entries"].values())
        assert r["stop_loss"] > 0


def test_precio_invalido_lanza_error():
    with pytest.raises(ValueError):
        compute_entry_exit(current_price=0, technicals={"atr_14": 1.0})
    with pytest.raises(ValueError):
        compute_entry_exit(current_price=None, technicals={"atr_14": 1.0})


def test_rr_solo_favorable_si_el_riesgo_es_real():
    r = compute_entry_exit(current_price=100.0, technicals={"atr_14": 2.0}, score=7.0)
    for combo in r["risk_reward"].values():
        if combo["favorable"]:
            assert combo["risk"] > 0
            assert combo["reward"] > 0
            assert 2.0 <= combo["rr_ratio"] <= 15.0


def test_formato_no_revienta_con_warnings():
    r = compute_entry_exit(current_price=100.0, technicals={"atr_14": 50.0}, score=5.0)
    texto = format_entry_exit(r, ticker="TEST")
    assert "STOP LOSS" in texto
    assert "⚠" in texto
