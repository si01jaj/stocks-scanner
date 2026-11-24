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
  GET  /api/history         Histórico de señales y acierto por horizonte
  POST /api/run             Lanzar el screener en segundo plano → job_id
  GET  /api/run/{job_id}    Estado del escaneo
"""
import os, sys, json, glob, asyncio, time, threading, uuid
from collections import deque
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

# Token opcional: si SCANNER_API_TOKEN está definido, POST /api/run lo exige.
# Con la variable vacía el endpoint queda abierto, como antes, pero el puerto
# está publicado sólo en loopback (ver docker-compose.yml).
API_TOKEN = os.environ.get("SCANNER_API_TOKEN", "").strip()

# ─── Estado de ejecuciones ────────────────────────────────────
# Un sólo escaneo a la vez: dos scans concurrentes escribían en el mismo
# data/reports/ y en el mismo ledger.
_scan_lock = threading.Lock()
_jobs = {}
_jobs_order = deque(maxlen=20)


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


def _run_job(job_id):
    """Ejecuta el scan en un worker y deja el resultado en el estado del job."""
    job = _jobs.setdefault(job_id, {})
    try:
        resultado = _run_screener_sync()
        if resultado:
            job.update(
                status="ok",
                finished=datetime.now().isoformat(timespec="seconds"),
                report={
                    "fecha": resultado.get("fecha"),
                    "alcistas": resultado.get("alcistas", []),
                    "bajistas": resultado.get("bajistas", []),
                    "candidatos_brutos": resultado.get("candidatos_brutos", 0),
                    "stats": resultado.get("stats", {}),
                },
            )
        else:
            job.update(status="error", error="No se pudieron obtener datos",
                       finished=datetime.now().isoformat(timespec="seconds"))
    except Exception as e:
        job.update(status="error", error=str(e),
                   finished=datetime.now().isoformat(timespec="seconds"))
    finally:
        try:
            _scan_lock.release()
        except RuntimeError:
            pass


# ─── App Setup ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure dirs exist
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(os.path.join(_project_root, "logs"), exist_ok=True)
    yield

app = FastAPI(title="Stocks Scanner API", version="1.0.0", lifespan=lifespan)
templates_dir = os.path.join(_project_root, "api", "templates")
templates = Jinja2Templates(directory=templates_dir)

STATIC_DIR = os.path.join(_project_root, "api", "static")
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

    return templates.TemplateResponse(request, "index.html", {
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
    return templates.TemplateResponse(request, "scan_detail.html", {
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

    return templates.TemplateResponse(request, "ticker.html", {
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


@app.get("/api/history")
async def api_history():
    """
    Histórico persistente de señales y su resultado posterior.

    Es la pieza que faltaba para poder decir si el scoring acierta: hit-rate,
    retorno medio y MFE/MAE por tipo y horizonte (5/20/60 sesiones).
    """
    try:
        from scripts.signals_ledger import diagnostico, stats
        return {"diagnostico": diagnostico(), "stats": stats()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/run", status_code=202)
async def api_run_screener(request: Request):
    """
    Lanza el screener en segundo plano y devuelve un job_id al instante.

    Antes la petición esperaba los minutos que duraba el scan (el docstring
    prometía "returns immediately with a task ID" pero no era cierto) y no
    había nada que impidiera dos escaneos simultáneos.
    """
    if API_TOKEN and request.headers.get("x-api-token") != API_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not _scan_lock.acquire(blocking=False):
        return JSONResponse(
            {"status": "already_running", "error": "Ya hay un escaneo en curso"},
            status_code=409,
        )

    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    _jobs_order.append(job_id)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_job, job_id)

    return {"job_id": job_id, "status": "running", "poll": f"/api/run/{job_id}"}


@app.get("/api/run/{job_id}")
async def api_run_status(job_id: str):
    """Estado de un escaneo lanzado con POST /api/run."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": f"job no encontrado: {job_id}"}, status_code=404)
    return job


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=True)
