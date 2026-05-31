"""
FastAPI Application — Stocks Scanner Dashboard + API.

Endpoints HTML:
  GET  /                    Dashboard principal
  GET  /scan/{fecha}        Detalle de un scan específico
  GET  /ticker/{symbol}     Histórico de un ticker

Endpoints JSON:
  GET  /api/reports         Lista de reportes disponibles
  GET  /api/reports/{fecha} Reporte de una fecha específica
  GET  /api/ticker/{symbol} Datos históricos de un ticker
  GET  /api/status          Estado del sistema
  POST /api/run             Ejecutar el screener
"""
import os, sys, json, glob, asyncio, time
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

_project_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

REPORTS_DIR = os.path.join(_project_root, "data", "reports")


# ─── Helpers ─────────────────────────────────────────────────

def _list_report_files():
    if not os.path.exists(REPORTS_DIR):
        return []
    return sorted(glob.glob(os.path.join(REPORTS_DIR, "scan_*.json")), reverse=True)


def _parse_report_meta(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        basename = os.path.basename(filepath)
        fecha = data.get("fecha", basename[5:15])
        alcistas = data.get("alcistas", [])
        bajistas = data.get("bajistas", [])
        return {
            "filename": basename,
            "path": filepath,
            "fecha": fecha,
            "scan_date": data.get("scan_date", ""),
            "alcistas_count": len(alcistas),
            "bajistas_count": len(bajistas),
            "candidatos": data.get("candidatos_brutos", 0),
            "top_alcista": alcistas[0]["ticker"] if alcistas else "—",
            "top_bajista": bajistas[0]["ticker"] if bajistas else "—",
        }
    except Exception:
        return None


def _load_report_by_date(fecha):
    pattern = os.path.join(REPORTS_DIR, f"scan_{fecha}*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_latest_report():
    files = _list_report_files()
    if not files:
        return None
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _build_chart_data():
    """Build chart data from the last N reports."""
    files = _list_report_files()
    # Take last 5
    recent = files[:5]
    recent.reverse()
    labels = []
    alcistas = []
    bajistas = []
    for f in recent:
        meta = _parse_report_meta(f)
        if meta:
            labels.append(meta["fecha"][-5:] if len(meta["fecha"]) > 5 else meta["fecha"])
            alcistas.append(meta["alcistas_count"])
            bajistas.append(meta["bajistas_count"])
    if not labels:
        return None
    return {
        "labels": labels,
        "alcistas": alcistas,
        "bajistas": bajistas,
    }


def _get_ticker_history(ticker):
    """Get historical data for a ticker across all reports."""
    entries = []
    files = _list_report_files()
    for f in reversed(files):
        try:
            with open(f, "r", encoding="utf-8") as rp:
                data = json.load(rp)
        except Exception:
            continue

        fecha = data.get("fecha", "")
        for lista_name in ("alcistas", "bajistas"):
            for c in data.get(lista_name, []):
                if c.get("ticker", "").upper() == ticker.upper():
                    entries.append({
                        "fecha": fecha,
                        "lista": lista_name,
                        **c,
                    })
                    break
    return entries


def _run_screener_sync():
    """Run the screener synchronously and return the result."""
    sys.path.insert(0, _project_root)
    from scripts.run_screener import ejecutar_screener
    return ejecutar_screener(verbose=False)


# ─── App Setup ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure dirs exist
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(os.path.join(_project_root, "logs"), exist_ok=True)
    yield

app = FastAPI(title="Stocks Scanner API", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─── HTML Routes ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    reporte_actual = _get_latest_report()
    alcistas = reporte_actual.get("alcistas", []) if reporte_actual else []
    bajistas = reporte_actual.get("bajistas", []) if reporte_actual else []

    files = _list_report_files()
    reportes = []
    for f in files:
        meta = _parse_report_meta(f)
        if meta:
            reportes.append(meta)

    chart_data = _build_chart_data()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "alcistas": alcistas,
        "bajistas": bajistas,
        "reporte_actual": reporte_actual,
        "reportes": reportes,
        "chart_data": chart_data,
    })


@app.get("/scan/{fecha}", response_class=HTMLResponse)
async def scan_detail(request: Request, fecha: str):
    reporte = _load_report_by_date(fecha)
    if not reporte:
        return HTMLResponse(
            "<div class='p-12 text-center text-gray-400'>"
            "<p class='text-4xl mb-3'>📭</p>"
            "<p>No se encontró scan para la fecha {{ fecha }}</p>"
            "<a href='/' class='text-sm underline mt-4 inline-block'>Volver</a></div>",
            status_code=404,
        )
    return templates.TemplateResponse("scan_detail.html", {
        "request": request,
        "fecha": fecha,
        "reporte": reporte,
    })


@app.get("/ticker/{symbol}", response_class=HTMLResponse)
async def ticker_detail(request: Request, symbol: str):
    ticker = symbol.upper()
    entries = _get_ticker_history(ticker)
    name = ""
    tendencia = "neutro"
    if entries:
        name = entries[0].get("name", "")
        alcista_count = sum(1 for e in entries if e.get("clasificacion", {}).get("tipo") == "ALCISTA")
        bajista_count = sum(1 for e in entries if e.get("clasificacion", {}).get("tipo") == "BAJISTA")
        if alcista_count > bajista_count:
            tendencia = "alcista"
        elif bajista_count > alcista_count:
            tendencia = "bajista"

    return templates.TemplateResponse("ticker.html", {
        "request": request,
        "ticker": ticker,
        "name": name,
        "entries": entries,
        "tendencia": tendencia,
    })


# ─── JSON API Routes ──────────────────────────────────────────

@app.get("/api/reports")
async def api_reports():
    files = _list_report_files()
    reportes = []
    for f in files:
        meta = _parse_report_meta(f)
        if meta:
            reportes.append(meta)
    return {"reports": reportes}


@app.get("/api/reports/{fecha}")
async def api_report_by_date(fecha: str):
    reporte = _load_report_by_date(fecha)
    if not reporte:
        return JSONResponse({"error": f"No report for date: {fecha}"}, status_code=404)
    return reporte


@app.get("/api/ticker/{symbol}")
async def api_ticker(symbol: str):
    entries = _get_ticker_history(symbol.upper())
    return {"ticker": symbol.upper(), "entries": entries}


@app.get("/api/status")
async def api_status():
    files = _list_report_files()
    latest = _get_latest_report()
    return {
        "status": "ok",
        "reports_count": len(files),
        "latest_scan": latest.get("scan_date", "") if latest else None,
        "latest_fecha": latest.get("fecha", "") if latest else None,
    }


@app.post("/api/run")
async def api_run_screener():
    """
    Execute the screener in a background thread.
    Returns immediately with a task ID, result available via /api/status.
    """
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_screener_sync)
        if result:
            return {
                "status": "ok",
                "message": "Scan completado",
                "report": {
                    "fecha": result.get("fecha"),
                    "alcistas": result.get("alcistas", []),
                    "bajistas": result.get("bajistas", []),
                    "candidatos_brutos": result.get("candidatos_brutos", 0),
                    "stats": result.get("stats", {}),
                },
            }
        else:
            return JSONResponse({"status": "error", "error": "No se pudieron obtener datos"}, status_code=500)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=True)
