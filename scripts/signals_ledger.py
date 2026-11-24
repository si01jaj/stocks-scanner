#!/usr/bin/env python3
"""
Signal Ledger — histórico persistente de señales y su resultado posterior.

El scanner guardaba snapshots JSON (5 ficheros, borrados al escanear) y con
eso era imposible responder a la única pregunta que importa: ¿acierta?

Este módulo añade la pieza mínima: cada señal (ticker + día + clasificación +
precio de referencia) se guarda en SQLite, y un proceso posterior rellena su
rentabilidad a 5 / 20 / 60 sesiones junto con el máximo avance y retroceso
del periodo (MFE / MAE). Con eso se puede medir hit-rate por banda de puntos
sin montar un backtester completo.

Uso:
    from scripts.signals_ledger import record_scan, update_outcomes, stats

    record_scan(resultado_del_screener)   # lo llama run_screener.py
    update_outcomes()                     # rellena horizontes ya vencidos
    print(stats())                        # agrega por tipo y horizonte

CLI:
    python scripts/signals_ledger.py init
    python scripts/signals_ledger.py record data/reports/scan_2026-11-24_101500.json
    python scripts/signals_ledger.py update
    python scripts/signals_ledger.py stats
"""
import os
import sys
import json
import sqlite3
import statistics
from datetime import datetime, date

_project_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

DB_PATH = os.environ.get("SCANNER_DB_PATH", os.path.join(_project_root, "data", "signals.db"))
HORIZONS = (5, 20, 60)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date       TEXT NOT NULL,           -- ISO completo del escaneo
    scan_day        TEXT NOT NULL,           -- YYYY-MM-DD
    ticker          TEXT NOT NULL,
    tipo            TEXT NOT NULL,           -- ALCISTA | BAJISTA
    nivel           TEXT,
    puntos         INTEGER,
    max_posible     INTEGER,                 -- techo teórico del modelo (17)
    max_disponible  INTEGER,                 -- techo evaluable con los datos del candidato
    margen          INTEGER,
    score           REAL,
    precio          REAL,                    -- precio de referencia al publicar
    rsi             REAL,
    senales         TEXT,                    -- JSON con las etiquetas de señal
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scan_day, ticker)
);

CREATE TABLE IF NOT EXISTS outcomes (
    signal_id           INTEGER NOT NULL,
    horizon             INTEGER NOT NULL,    -- sesiones: 5, 20, 60
    exit_date           TEXT,
    exit_price          REAL,
    ret_pct             REAL,
    max_favorable_pct   REAL,                -- MFE: mejor momento del periodo
    max_adverse_pct     REAL,                -- MAE: peor momento del periodo
    filled_at           TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (signal_id, horizon),
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_day ON signals(scan_day);
"""


# ─── Conexión / esquema ────────────────────────────────────────────

def connect(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=None):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return db_path or DB_PATH


def _migrate(conn):
    """
    Migraciones incrementales sobre una BD ya existente.

    `max_disponible` se añadió después: sin él no se puede normalizar
    7/10 frente a 7/12, y el histórico de señales viejas quedaría sin
    denominador comparable.
    """
    columnas = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
    if "max_disponible" not in columnas:
        conn.execute("ALTER TABLE signals ADD COLUMN max_disponible INTEGER")


# ─── Ingesta ───────────────────────────────────────────────────────

def record_scan(resultado, db_path=None):
    """
    Registra las señales de un resultado de `ejecutar_screener()`.

    Idempotente por (día, ticker): volver a escanear el mismo día actualiza
    la fila en lugar de duplicarla. Devuelve el número de señales escritas.
    """
    if not resultado:
        return 0

    scan_date = resultado.get("scan_date") or datetime.now().isoformat()
    try:
        scan_day = datetime.fromisoformat(scan_date).date().isoformat()
    except (ValueError, TypeError):
        scan_day = date.today().isoformat()

    escritas = 0
    path = init_db(db_path)
    with connect(path) as conn:
        for tipo_key, lista in (("alcistas", resultado.get("alcistas", [])),
                                ("bajistas", resultado.get("bajistas", []))):
            for c in lista:
                cl = c.get("clasificacion") or {}
                if not c.get("ticker") or not cl.get("tipo"):
                    continue
                conn.execute(
                    """
                    INSERT INTO signals (scan_date, scan_day, ticker, tipo, nivel,
                                         puntos, max_posible, max_disponible, margen,
                                         score, precio, rsi, senales)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scan_day, ticker) DO UPDATE SET
                        tipo=excluded.tipo, nivel=excluded.nivel,
                        puntos=excluded.puntos, max_posible=excluded.max_posible,
                        max_disponible=excluded.max_disponible,
                        margen=excluded.margen, score=excluded.score,
                        precio=excluded.precio, rsi=excluded.rsi,
                        senales=excluded.senales
                    """,
                    (
                        scan_date, scan_day, c.get("ticker").upper(), cl.get("tipo"),
                        cl.get("nivel"), cl.get("puntos"), cl.get("max_posible"),
                        cl.get("max_disponible"), cl.get("margen"), c.get("score"),
                        c.get("precio"), c.get("rsi"),
                        json.dumps(cl.get("senales", []), ensure_ascii=False),
                    ),
                )
                escritas += 1
    return escritas


# ─── Relleno de horizontes ─────────────────────────────────────────

def default_fetcher(ticker):
    """
    Devuelve el histórico diario de un ticker como lista de dicts
    {date, high, low, close}, ordenada de más antigua a más reciente.
    """
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="1y")
    if hist is None or hist.empty:
        return []
    out = []
    for idx, row in hist.iterrows():
        out.append({
            "date": idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10],
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        })
    return out


def _pending(conn, horizons):
    """Señales con el precio de referencia válido y algún horizonte sin rellenar."""
    rows = conn.execute(
        "SELECT id, scan_day, ticker, precio FROM signals WHERE precio IS NOT NULL AND precio > 0"
    ).fetchall()
    pendientes = []
    for row in rows:
        hechos = {
            r["horizon"] for r in conn.execute(
                "SELECT horizon FROM outcomes WHERE signal_id = ?", (row["id"],)
            )
        }
        faltan = [h for h in horizons if h not in hechos]
        if faltan:
            pendientes.append((dict(row), faltan))
    return pendientes


def _evaluate(precio_entrada, scan_day, bars, horizon):
    """
    Calcula el resultado de una señal a `horizon` sesiones.

    Regla: la primera sesión POSTERIOR al día de la señal es la sesión 1 (no
    se puede comprar con el cierre del día en que se publica la señal).
    Devuelve None si todavía no han pasado esas sesiones.
    """
    posteriores = [b for b in bars if b["date"] > scan_day]
    if len(posteriores) < horizon:
        return None

    ventana = posteriores[:horizon]
    salida = ventana[-1]
    mfe = max(b["high"] for b in ventana)
    mae = min(b["low"] for b in ventana)

    return {
        "exit_date": salida["date"],
        "exit_price": round(salida["close"], 2),
        "ret_pct": round((salida["close"] / precio_entrada - 1) * 100, 2),
        "max_favorable_pct": round((mfe / precio_entrada - 1) * 100, 2),
        "max_adverse_pct": round((mae / precio_entrada - 1) * 100, 2),
    }


def update_outcomes(db_path=None, fetcher=None, horizons=HORIZONS, today=None):
    """
    Rellena los horizontes ya vencidos. Devuelve {filas_escritas, pendientes,
    errores}. `fetcher` es inyectable para poder testear sin red.
    """
    fetcher = fetcher or default_fetcher
    hoy = (today or date.today()).isoformat()
    path = init_db(db_path)
    escritas = 0
    pendientes = 0
    errores = []

    cache = {}
    with connect(path) as conn:
        for signal, faltan in _pending(conn, horizons):
            ticker = signal["ticker"]
            if ticker not in cache:
                try:
                    cache[ticker] = fetcher(ticker) or []
                except Exception as e:  # proveedor caído: se reintenta luego
                    cache[ticker] = []
                    errores.append(f"{ticker}: {e}")
            bars = [b for b in cache[ticker] if b["date"] <= hoy]
            if not bars:
                pendientes += 1
                continue

            for horizon in faltan:
                resultado = _evaluate(signal["precio"], signal["scan_day"], bars, horizon)
                if resultado is None:
                    pendientes += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO outcomes (signal_id, horizon, exit_date, exit_price,
                                          ret_pct, max_favorable_pct, max_adverse_pct,
                                          filled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(signal_id, horizon) DO UPDATE SET
                        exit_date=excluded.exit_date, exit_price=excluded.exit_price,
                        ret_pct=excluded.ret_pct,
                        max_favorable_pct=excluded.max_favorable_pct,
                        max_adverse_pct=excluded.max_adverse_pct,
                        filled_at=excluded.filled_at
                    """,
                    (
                        signal["id"], horizon, resultado["exit_date"],
                        resultado["exit_price"], resultado["ret_pct"],
                        resultado["max_favorable_pct"], resultado["max_adverse_pct"],
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                escritas += 1

    return {"filas_escritas": escritas, "pendientes": pendientes, "errores": errores}


# ─── Estadística ───────────────────────────────────────────────────

def stats(db_path=None, horizons=HORIZONS):
    """
    Agrega por (tipo, horizonte): n, hit-rate (ret > 0), ret medio y mediano,
    MFE/MAE medios. Para un bajista, "acertar" es que el precio caiga.
    """
    path = init_db(db_path)
    salida = {}
    with connect(path) as conn:
        for tipo in ("ALCISTA", "BAJISTA"):
            for horizon in horizons:
                rows = conn.execute(
                    """
                    SELECT s.tipo, s.puntos, s.max_disponible,
                           o.ret_pct, o.max_favorable_pct, o.max_adverse_pct
                    FROM outcomes o JOIN signals s ON s.id = o.signal_id
                    WHERE s.tipo = ? AND o.horizon = ?
                    """,
                    (tipo, horizon),
                ).fetchall()
                if not rows:
                    continue
                rets = [r["ret_pct"] for r in rows]
                aciertos = sum(1 for r in rets if (r > 0) == (tipo == "ALCISTA"))
                ratios = [
                    r["puntos"] / r["max_disponible"] * 100
                    for r in rows
                    if r["puntos"] is not None and r["max_disponible"]
                ]
                puntos = [r["puntos"] for r in rows if r["puntos"] is not None]
                salida[f"{tipo}@{horizon}"] = {
                    "n": len(rets),
                    "hit_rate": round(aciertos / len(rets) * 100, 1),
                    "ret_medio": round(statistics.fmean(rets), 2),
                    "ret_mediano": round(statistics.median(rets), 2),
                    "mfe_medio": round(statistics.fmean(r["max_favorable_pct"] for r in rows), 2),
                    "mae_medio": round(statistics.fmean(r["max_adverse_pct"] for r in rows), 2),
                    # Normalizado por el techo evaluable de cada candidato
                    "puntos_medio": round(statistics.fmean(puntos), 1) if puntos else None,
                    "ratio_medio": round(statistics.fmean(ratios), 1) if ratios else None,
                }
    return salida


def diagnostico(db_path=None):
    """Resumen de estado del histórico (útil para el dashboard y para el CLI)."""
    path = init_db(db_path)
    with connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
        por_tipo = {
            r["tipo"]: r["c"] for r in conn.execute(
                "SELECT tipo, COUNT(*) c FROM signals GROUP BY tipo"
            )
        }
        rellenos = conn.execute("SELECT COUNT(*) c FROM outcomes").fetchone()["c"]
        rango = conn.execute(
            "SELECT MIN(scan_day) a, MAX(scan_day) b FROM signals"
        ).fetchone()
    return {
        "senales": total,
        "por_tipo": por_tipo,
        "outcomes": rellenos,
        "desde": rango["a"],
        "hasta": rango["b"],
    }


# ─── CLI ───────────────────────────────────────────────────────────

def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]

    if cmd == "init":
        print(f"Ledger inicializado en {init_db()}")
        print(diagnostico())
        return 0

    if cmd == "record":
        if len(argv) < 2:
            print("Uso: signals_ledger.py record <reporte.json>")
            return 2
        with open(argv[1], "r", encoding="utf-8") as f:
            resultado = json.load(f)
        n = record_scan(resultado)
        print(f"{n} señales registradas. Estado: {diagnostico()}")
        return 0

    if cmd == "update":
        print(update_outcomes())
        return 0

    if cmd == "stats":
        res = stats()
        if not res:
            print("Sin resultados todavía: ejecuta `update` cuando venzan los horizontes.")
            return 0
        print(f"{'grupo':<14} {'n':>4} {'hit%':>7} {'medio%':>8} {'mediano%':>9} "
              f"{'MFE%':>7} {'MAE%':>7} {'pts':>5} {'ratio%':>7}")
        for k, v in sorted(res.items()):
            print(f"{k:<14} {v['n']:>4} {v['hit_rate']:>7} {v['ret_medio']:>8} "
                  f"{v['ret_mediano']:>9} {v['mfe_medio']:>7} {v['mae_medio']:>7} "
                  f"{v['puntos_medio'] if v['puntos_medio'] is not None else '—':>5} "
                  f"{v['ratio_medio'] if v['ratio_medio'] is not None else '—':>7}")
        return 0

    print(f"Comando desconocido: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
