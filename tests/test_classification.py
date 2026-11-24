"""Tests de clasificación y scoring del motor (scripts/run_screener.py).

Cubren las correcciones:
  - máximo de puntos derivado de los pesos (no 18 fijo)
  - sin señal `score_alto` (doble contabilización circular)
  - volumen con dirección obligatoria
  - empate → neutro
  - RSI extremo que no invierte la clasificación
  - score como filtro de régimen
"""
import pytest

from scripts.run_screener import (
    MAX_BEARISH_SCORE,
    MAX_BULLISH_SCORE,
    SIGNALS_ALCISTAS,
    SIGNALS_BAJISTAS,
    SIGNAL_LABELS,
    SIGNAL_WEIGHTS,
    analizar_senales_alcistas,
    analizar_senales_bajistas,
    clasificar_candidato,
    estimar_score,
    motivo_descarte,
)


def candidato_alcista(**overrides):
    """Estructura alcista completa, con score alto."""
    datos = {
        "sobre_sma50": True,
        "sobre_sma200": True,
        "macd_bullish": True,
        "golden_cross": True,
        "rsi_14": 50,
        "volume_ratio": 1.0,
        "variacion_pct_1d": 0.5,
        "tradingview": {"recommendation": "BUY"},
    }
    datos.update(overrides)
    return datos


# ─── Invariantes del modelo de puntos ──────────────────────────────

def test_maximos_se_derivan_de_los_pesos():
    assert MAX_BULLISH_SCORE == sum(SIGNAL_WEIGHTS[s] for s in SIGNALS_ALCISTAS)
    assert MAX_BEARISH_SCORE == sum(SIGNAL_WEIGHTS[s] for s in SIGNALS_BAJISTAS)
    # El techo real de cada bando, no el 18 fijo de antes
    assert MAX_BULLISH_SCORE == 17
    assert MAX_BEARISH_SCORE == 17


def test_el_score_ya_no_puntua_como_senal():
    """score_alto/score_bajo eran el mismo hecho contado dos veces."""
    assert "score_alto" not in SIGNAL_WEIGHTS
    assert "score_bajo" not in SIGNAL_WEIGHTS
    assert "score_alto" not in SIGNAL_LABELS
    assert "score_bajo" not in SIGNAL_LABELS


@pytest.mark.parametrize("datos", [
    candidato_alcista(),
    candidato_alcista(rsi_14=80),
    candidato_alcista(sobre_sma50=False, sobre_sma200=False, macd_bullish=False,
                      golden_cross=False, tradingview={"recommendation": "SELL"}),
])
def test_toda_senal_emitida_tiene_peso_y_etiqueta(datos):
    for _, senales in (analizar_senales_alcistas(datos), analizar_senales_bajistas(datos)):
        for s in senales:
            assert s in SIGNAL_WEIGHTS, f"señal sin peso: {s}"
            assert s in SIGNAL_LABELS, f"señal sin etiqueta: {s}"


# ─── Volumen con dirección ────────────────────────────────────────

def test_volumen_alto_en_subida_no_puntua_al_bajista():
    datos = candidato_alcista(volume_ratio=1.5, variacion_pct_1d=2.0)
    _, senales_alcista = analizar_senales_alcistas(datos)
    _, senales_bajista = analizar_senales_bajistas(datos)
    assert "vol_alto" in senales_alcista
    assert "vol_alto_bajista" not in senales_bajista


def test_volumen_alto_en_caida_no_puntua_al_alcista():
    datos = candidato_alcista(volume_ratio=1.5, variacion_pct_1d=-3.0)
    _, senales_alcista = analizar_senales_alcistas(datos)
    _, senales_bajista = analizar_senales_bajistas(datos)
    assert "vol_alto" not in senales_alcista
    assert "vol_alto_bajista" in senales_bajista


def test_volumen_sin_direccion_conocida_no_puntua():
    datos = candidato_alcista(volume_ratio=1.5, variacion_pct_1d=None)
    assert analizar_senales_alcistas(datos)[0] == 9   # 2+2+2+1 + 2 TV, sin volumen
    assert "vol_alto_bajista" not in analizar_senales_bajistas(datos)[1]


def test_volumen_normal_no_puntua():
    datos = candidato_alcista(volume_ratio=0.9, variacion_pct_1d=-3.0)
    assert "vol_alto" not in analizar_senales_alcistas(datos)[1]
    assert "vol_alto_bajista" not in analizar_senales_bajistas(datos)[1]


# ─── Clasificación ────────────────────────────────────────────────

def test_candidato_alcista_completo_se_clasifica():
    datos = candidato_alcista()
    datos["score_estimado"] = estimar_score(datos)
    cl = clasificar_candidato(datos)
    assert cl is not None
    assert cl["tipo"] == "ALCISTA"
    assert cl["puntos"] == 9          # 2+2+2+1 + 2 (TradingView)
    assert cl["max_posible"] == MAX_BULLISH_SCORE
    assert cl["margen"] >= 1


def test_empate_es_neutro_no_alcista(monkeypatch):
    """Antes `puntos_alcista >= puntos_bajista` resolvía el empate a favor del alza."""
    import scripts.run_screener as rs

    monkeypatch.setattr(rs, "analizar_senales_alcistas", lambda d: (9, ["sobre_sma50"]))
    monkeypatch.setattr(rs, "analizar_senales_bajistas", lambda d: (9, ["bajo_sma50"]))

    datos = {"score_estimado": 6.0}
    assert rs.clasificar_candidato(datos) is None
    assert "empate" in rs.motivo_descarte(datos)


def test_pocas_senales_no_bastan():
    """Estructura de tendencia sola (5 puntos) no llega al umbral de 6."""
    datos = candidato_alcista(tradingview=None, macd_bullish=False)
    datos["score_estimado"] = 8.0
    assert analizar_senales_alcistas(datos)[0] == 5
    assert clasificar_candidato(datos) is None
    assert "insuficientes" in motivo_descarte(datos)


def test_max_disponible_refleja_los_datos_presentes():
    """Sin TradingView ni insider/analistas no se pueden evaluar 17 puntos."""
    from scripts.run_screener import max_puntos_disponibles

    datos = candidato_alcista(tradingview=None)
    assert max_puntos_disponibles(datos, alcista=True) == 10   # 2+2+2+2+1+1
    with_tv = candidato_alcista()
    assert max_puntos_disponibles(with_tv, alcista=True) == 12
    assert max_puntos_disponibles(with_tv, alcista=False) == 12


def test_score_bajo_bloquea_la_clasificacion_alcista():
    datos = candidato_alcista()
    datos["score_estimado"] = 3.0     # muchas señales pero régimen malo
    assert clasificar_candidato(datos) is None


def test_score_alto_bloquea_la_clasificacion_bajista():
    datos = {
        "sobre_sma50": False, "sobre_sma200": False, "macd_bullish": False,
        "golden_cross": False, "rsi_14": 70, "volume_ratio": 1.5,
        "variacion_pct_1d": -2.0, "tradingview": {"recommendation": "SELL"},
        "score_estimado": 6.5,
    }
    assert clasificar_candidato(datos) is None
    datos["score_estimado"] = 2.0
    cl = clasificar_candidato(datos)
    assert cl is not None and cl["tipo"] == "BAJISTA"


# ─── Sobrecompra extrema ─────────────────────────────────────────

def test_rsi_extremo_no_convierte_un_lider_en_bajista():
    """
    Con la penalización triple anterior, RSI 80 dejaba el score en 4.5 y el
    valor pasaba a BAJISTA por haber subido demasiado.
    """
    datos = candidato_alcista(rsi_14=80, tradingview=None)
    datos["score_estimado"] = estimar_score(datos)
    assert datos["score_estimado"] == pytest.approx(5.5)
    cl = clasificar_candidato(datos)
    assert cl is None or cl["tipo"] != "BAJISTA"


def test_rsi_extremo_penaliza_una_sola_vez():
    normal = estimar_score(candidato_alcista(rsi_14=60))
    sobrecomprado = estimar_score(candidato_alcista(rsi_14=70))
    extremo = estimar_score(candidato_alcista(rsi_14=80))
    # 5 + 1.5 (SMA200) + 1.0 (SMA50) + 1.0 (MACD) + 1.0 (TradingView BUY)
    assert normal == pytest.approx(9.5)
    assert sobrecomprado == pytest.approx(8.0)   # -1.5
    assert extremo == pytest.approx(6.5)         # -3.0 (no -2 y -2 y -3)
