"""Tests del histórico de señales (scripts/signals_ledger.py)."""
import pytest

from scripts.signals_ledger import (
    diagnostico,
    record_scan,
    stats,
    update_outcomes,
)


def _reporte(scan_date="2026-01-01T10:00:00", precio=100.0):
    return {
        "scan_date": scan_date,
        "fecha": scan_date[:10],
        "alcistas": [{
            "ticker": "AAA",
            "precio": precio,
            "score": 7.5,
            "rsi": 55.0,
            "clasificacion": {"tipo": "ALCISTA", "nivel": "ALCISTA", "puntos": 9,
                              "max_posible": 17, "margen": 9, "senales": ["Precio > SMA50"]},
        }],
        "bajistas": [{
            "ticker": "BBB",
            "precio": 50.0,
            "score": 3.0,
            "rsi": 70.0,
            "clasificacion": {"tipo": "BAJISTA", "nivel": "BAJISTA", "puntos": 10,
                              "max_posible": 17, "margen": 10, "senales": ["RSI > 65"]},
        }],
    }


BARRAS_AAA = [
    {"date": "2026-01-02", "high": 103.0, "low": 99.0, "close": 101.0},
    {"date": "2026-01-05", "high": 106.0, "low": 98.0, "close": 102.0},
    {"date": "2026-01-06", "high": 107.0, "low": 100.0, "close": 103.0},
    {"date": "2026-01-07", "high": 110.0, "low": 101.0, "close": 106.0},
]

BARRAS_BBB = [
    {"date": "2026-01-02", "high": 51.0, "low": 47.0, "close": 48.0},
    {"date": "2026-01-05", "high": 49.0, "low": 44.0, "close": 45.0},
    {"date": "2026-01-06", "high": 46.0, "low": 42.0, "close": 43.0},
    {"date": "2026-01-07", "high": 44.0, "low": 40.0, "close": 41.0},
]


def _fetcher(barras_por_ticker):
    return lambda ticker: barras_por_ticker.get(ticker, [])


def _db(tmp_path):
    return str(tmp_path / "signals.db")


def test_record_scan_guarda_las_senales(tmp_path):
    db = _db(tmp_path)
    assert record_scan(_reporte(), db_path=db) == 2
    info = diagnostico(db_path=db)
    assert info["senales"] == 2
    assert info["por_tipo"] == {"ALCISTA": 1, "BAJISTA": 1}


def test_record_scan_es_idempotente(tmp_path):
    db = _db(tmp_path)
    record_scan(_reporte(), db_path=db)
    record_scan(_reporte(), db_path=db)  # mismo día y tickers
    assert diagnostico(db_path=db)["senales"] == 2


def test_update_calcula_retorno_mfe_y_mae(tmp_path):
    db = _db(tmp_path)
    record_scan(_reporte(), db_path=db)

    fetcher = _fetcher({"AAA": BARRAS_AAA, "BBB": BARRAS_BBB})
    res = update_outcomes(db_path=db, fetcher=fetcher, horizons=(3,), today=None)
    assert res["filas_escritas"] == 2

    from scripts.signals_ledger import connect
    with connect(db) as conn:
        row = conn.execute(
            """SELECT o.* FROM outcomes o JOIN signals s ON s.id = o.signal_id
               WHERE s.ticker = 'AAA' AND o.horizon = 3"""
        ).fetchone()

    assert row["exit_date"] == "2026-01-06"
    assert row["exit_price"] == 103.0
    assert row["ret_pct"] == pytest.approx(3.0)        # 103 / 100
    assert row["max_favorable_pct"] == pytest.approx(7.0)   # máximo 107
    assert row["max_adverse_pct"] == pytest.approx(-2.0)    # mínimo 98


def test_no_usa_el_precio_del_dia_de_la_senal(tmp_path):
    """La sesión 1 es la posterior: no se puede comprar al cierre de la señal."""
    db = _db(tmp_path)
    record_scan(_reporte(), db_path=db)
    barras = [
        {"date": "2026-01-01", "high": 999.0, "low": 999.0, "close": 999.0},  # día de la señal
    ] + BARRAS_AAA
    res = update_outcomes(db_path=db, fetcher=_fetcher({"AAA": barras, "BBB": []}),
                          horizons=(1,))
    assert res["filas_escritas"] == 1
    from scripts.signals_ledger import connect
    with connect(db) as conn:
        row = conn.execute(
            """SELECT o.exit_price, o.ret_pct FROM outcomes o
               JOIN signals s ON s.id = o.signal_id
               WHERE s.ticker = 'AAA' AND o.horizon = 1"""
        ).fetchone()
    assert row["exit_price"] == 101.0     # 2026-01-02, no 999
    assert row["ret_pct"] == pytest.approx(1.0)


def test_sin_sesiones_suficientes_queda_pendiente(tmp_path):
    db = _db(tmp_path)
    record_scan(_reporte(), db_path=db)
    res = update_outcomes(db_path=db, fetcher=_fetcher({"AAA": BARRAS_AAA[:2], "BBB": []}),
                          horizons=(5,))
    assert res["filas_escritas"] == 0
    assert res["pendientes"] >= 1


def test_proveedor_caido_no_rompe(tmp_path):
    db = _db(tmp_path)
    record_scan(_reporte(), db_path=db)

    def fetcher_roto(ticker):
        raise RuntimeError("proveedor caído")

    res = update_outcomes(db_path=db, fetcher=fetcher_roto, horizons=(5,))
    assert res["filas_escritas"] == 0
    assert len(res["errores"]) == 2


def test_stats_calcula_hit_rate(tmp_path):
    db = _db(tmp_path)
    record_scan(_reporte(), db_path=db)
    update_outcomes(db_path=db, fetcher=_fetcher({"AAA": BARRAS_AAA, "BBB": BARRAS_BBB}),
                    horizons=(3,))

    res = stats(db_path=db, horizons=(3,))
    assert res["ALCISTA@3"]["n"] == 1
    assert res["ALCISTA@3"]["hit_rate"] == 100.0
    # El bajista acierta si el precio cae: 41 / 50
    assert res["BAJISTA@3"]["hit_rate"] == 100.0
    assert res["BAJISTA@3"]["ret_medio"] == pytest.approx(-14.0)   # 43 / 50
