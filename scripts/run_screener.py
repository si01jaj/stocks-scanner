#!/usr/bin/env python3
"""
Bullish/Bearish Stock Screener — Escanea, filtra, clasifica y reporta.

Pipeline:
  1. Escanear fuentes (RSS, Reddit, StockTwits) → lista de candidatos
  2. Quick-score cada candidato (yfinance + TradingView)
  3. Filtrar por liquidez (precio, volumen, market cap)
  4. Clasificar como ALCISTA / BAJISTA según señales técnicas
  5. Enriquecer top 5 de cada categoría con Finnhub (insider, analyst)
  6. Guardar snapshot en data/reports/ y limpiar los mayores a 5 días

Usage:
    python scripts/run_screener.py                           # escaneo completo
    python scripts/run_screener.py --tickers AAPL MSFT NVDA  # tickers específicos
    python scripts/run_screener.py --output                  # solo output JSON
"""
import os, sys, json, glob, time, argparse, traceback
from datetime import datetime, date, timedelta
from collections import defaultdict

_project_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scripts.api_caller import call_api, call_with_fallback
from scripts.api_config import load_config, is_api_available
from scripts.usage_tracker import get_tracker

REPORTS_DIR = os.path.join(_project_root, "data", "reports")
MAX_DAYS = 5

# ═══════════════════════════════════════════════════════════════════
#  PESOS PARA SEÑALES — cada señal suma puntos a la clasificación
# ═══════════════════════════════════════════════════════════════════

SIGNAL_WEIGHTS = {
    "score_alto": 3,
    "score_bajo": 3,
    "sobre_sma50": 2,
    "bajo_sma50": 2,
    "sobre_sma200": 2,
    "bajo_sma200": 2,
    "rsi_optimo": 2,
    "rsi_sobrecomprado": 2,
    "macd_bullish": 2,
    "macd_bearish": 2,
    "golden_cross": 1,
    "death_cross": 1,
    "vol_alto": 1,
    "vol_alto_bajista": 1,
    "tradingview_bull": 2,
    "tradingview_bear": 2,
    "insider_buy": 2,
    "insider_sell": 2,
    "sector_tailwind": 1,
    "sector_headwind": 1,
    "analyst_bullish": 2,
    "analyst_bearish": 2,
}

MAX_BULLISH_SCORE = 18
MAX_BEARISH_SCORE = 18
MIN_BULLISH_THRESHOLD = 8
MIN_BEARISH_THRESHOLD = 8
STRONG_THRESHOLD = 12

SIGNAL_LABELS = {
    "score_alto": "Score ≥ 6.5",
    "score_bajo": "Score ≤ 4.5",
    "sobre_sma50": "Precio > SMA50",
    "bajo_sma50": "Precio < SMA50",
    "sobre_sma200": "Precio > SMA200",
    "bajo_sma200": "Precio < SMA200",
    "rsi_optimo": "RSI 40-58",
    "rsi_sobrecomprado": "RSI > 58",
    "macd_bullish": "MACD alcista",
    "macd_bearish": "MACD bajista",
    "golden_cross": "Golden cross",
    "death_cross": "Death cross",
    "vol_alto": "Volumen > promedio",
    "vol_alto_bajista": "Volumen alto en caída",
    "tradingview_bull": "TradingView BUY+",
    "tradingview_bear": "TradingView SELL+",
    "insider_buy": "Insider buying neto",
    "insider_sell": "Insider selling neto",
    "sector_tailwind": "Sector tailwind",
    "sector_headwind": "Sector headwind",
    "analyst_bullish": "Analysts bullish (mayoría)",
    "analyst_bearish": "Analysts bearish (mayoría)",
}


# ═══════════════════════════════════════════════════════════════════
#  FASE 1: ESCANEO DE FUENTES
# ═══════════════════════════════════════════════════════════════════

def escanear_rss():
    """Escanear RSS feeds financieros → lista de tickers con menciones."""
    try:
        import feedparser as _fp
        import requests as _req
        from scripts.rss_feeds import FEEDS, extract_tickers, TICKER_BLACKLIST
    except ImportError:
        return []

    print("  [RSS] Escaneando feeds financieros...")
    tickers = defaultdict(int)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }
    feeds_scanned = 0
    for feed_id, feed_info in FEEDS.items():
        if feed_info.get("tier", 3) > 2:
            continue
        try:
            r = _req.get(feed_info["url"], headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            feed = _fp.parse(r.content)
            for entry in feed.entries[:10]:
                text = f"{entry.get('title', '')} {entry.get('summary', '')}"
                found = extract_tickers(text)
                for t in found:
                    if t not in TICKER_BLACKLIST and len(t) >= 2:
                        tickers[t] += 1
            feeds_scanned += 1
        except Exception:
            continue

    result = [(t, c) for t, c in tickers.items() if c >= 2]
    result.sort(key=lambda x: -x[1])
    print(f"  [RSS] {feeds_scanned} feeds, {len(result)} tickers encontrados")
    return result[:30]


def escanear_reddit():
    """Obtener tickers trending de Reddit via ApeWisdom."""
    print("  [Reddit] Obteniendo trending stocks...")
    try:
        import requests
        r = requests.get(
            "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])[:20]
            tickers = [(s["ticker"], s.get("mentions", 0)) for s in results if s.get("ticker")]
            print(f"  [Reddit] {len(tickers)} tickers trending")
            return tickers
    except Exception as e:
        print(f"  [Reddit] Error: {e}")
    return []


def escanear_stocktwits():
    """Obtener tickers trending de StockTwits."""
    print("  [StockTwits] Obteniendo trending tickers...")
    try:
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Accept": "application/json",
        }
        r = requests.get(
            "https://api.stocktwits.com/api/2/trending/symbols.json",
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            symbols = data.get("symbols", [])
            tickers = [(s["symbol"], s.get("watchlist_count", 0)) for s in symbols[:15]]
            print(f"  [StockTwits] {len(tickers)} tickers trending")
            return tickers
    except Exception as e:
        print(f"  [StockTwits] Error: {e}")
    return []


def es_ticker_valido(ticker):
    if not ticker or len(ticker) < 2 or len(ticker) > 5:
        return False
    if not ticker.isalpha():
        return False
    COMMON_WORDS = {
        "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN",
        "WAS", "ONE", "OUR", "OUT", "HAS", "HIS", "HOW", "ITS", "MAY", "NEW",
        "NOW", "OLD", "SEE", "WAY", "WHO", "DID", "GET", "LET", "SAY", "SHE",
        "TOO", "USE", "CEO", "IPO", "ETF", "GDP", "SEC", "FDA", "FED", "NYSE",
        "AI", "EV", "PE", "US", "UK", "EU", "AM", "PM", "VS", "BE", "BY", "DO",
        "GO", "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO",
        "TO", "UP", "WE", "BIG", "TOP", "LOW", "KEY", "SET", "RUN", "HIT",
        "CUT", "BUY", "PUT", "USD", "JPY", "EUR", "GBP", "CAD", "AUD",
    }
    return ticker not in COMMON_WORDS


def fusionar_candidatos(fuentes):
    """
    Fusiona listas de tickers de múltiples fuentes con scoring normalizado.
    Retorna lista de (ticker, menciones, num_fuentes, score_combinado).
    """
    todos = defaultdict(lambda: {"menciones": 0, "fuentes": set()})

    for nombre_fuente, lista in fuentes.items():
        if not lista:
            continue
        for ticker, menciones in lista:
            ticker = ticker.upper().strip()
            if not es_ticker_valido(ticker):
                continue
            todos[ticker]["menciones"] += menciones
            todos[ticker]["fuentes"].add(nombre_fuente)

    ranked = []
    for ticker, data in todos.items():
        num_fuentes = len(data["fuentes"])
        diversidad = (num_fuentes - 1) * 50
        score_total = diversidad + min(data["menciones"], 100)
        ranked.append((ticker, data["menciones"], num_fuentes, score_total, sorted(data["fuentes"])))

    ranked.sort(key=lambda x: -x[3])
    return ranked


# ═══════════════════════════════════════════════════════════════════
#  FASE 2: QUICK SCREEN + FILTRO LIQUIDEZ
# ═══════════════════════════════════════════════════════════════════

def obtener_datos_candidato(ticker):
    """
    Obtiene datos básicos de un ticker usando yfinance + TradingView.
    Retorna dict con toda la info necesaria para clasificar,
    o None si no se pudo obtener info básica.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info
        if not info or not isinstance(info, dict):
            return None

        hist = t.history(period="1y")
        if hist.empty or len(hist) < 20:
            return None
    except Exception:
        return None

    # Extraer datos fundamentales básicos
    fundamentos = {
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "name": info.get("longName", ticker),
        "precio": float(info.get("currentPrice") or info.get("regularMarketPrice") or hist["Close"].iloc[-1]),
    }

    # Si no hay precio, no podemos continuar
    if not fundamentos["precio"] or fundamentos["precio"] <= 0:
        return None

    # Análisis técnico básico (sin pandas-ta para ser ligeros)
    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    volume = hist["Volume"]
    latest_close = float(close.iloc[-1])

    # SMAs
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    # RSI manual
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = float((100 - (100 / (1 + rs))).iloc[-1]) if loss.iloc[-1] != 0 else 50

    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9).mean()
    macd_bullish = bool((macd_line.iloc[-1] - signal_line.iloc[-1]) > 0)

    # Volumen
    vol_avg_20 = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else 1
    vol_latest = int(volume.iloc[-1])
    volume_ratio = vol_latest / max(vol_avg_20, 1)

    # Precio min/max 52 semanas
    precio_52w_high = info.get("fiftyTwoWeekHigh") or float(high.max())
    precio_52w_low = info.get("fiftyTwoWeekLow") or float(low.min())

    # TradingView consensus
    tv_result = None
    try:
        from tradingview_ta import TA_Handler, Interval
        for exch in ["NASDAQ", "NYSE", "AMEX"]:
            try:
                handler = TA_Handler(
                    symbol=ticker, screener="america", exchange=exch,
                    interval=Interval.INTERVAL_1_DAY,
                )
                analysis = handler.get_analysis()
                if analysis and analysis.summary:
                    tv_result = {
                        "recommendation": analysis.summary.get("RECOMMENDATION", ""),
                        "buy": analysis.summary.get("BUY", 0),
                        "sell": analysis.summary.get("SELL", 0),
                        "neutral": analysis.summary.get("NEUTRAL", 0),
                    }
                    break
            except Exception:
                continue
    except ImportError:
        pass

    datos = {
        "ticker": ticker,
        "name": fundamentos["name"],
        "precio": round(fundamentos["precio"], 2),
        "market_cap": fundamentos["market_cap"],
        "sector": fundamentos.get("sector", ""),
        "industry": fundamentos.get("industry", ""),
        "sma50": round(sma50, 2) if sma50 else None,
        "sma200": round(sma200, 2) if sma200 else None,
        "sobre_sma50": latest_close > sma50 if sma50 else None,
        "sobre_sma200": latest_close > sma200 if sma200 else None,
        "golden_cross": sma50 > sma200 if sma50 and sma200 else None,
        "rsi_14": round(rsi, 1),
        "macd_bullish": macd_bullish,
        "volume_ratio": round(volume_ratio, 2),
        "vol_latest": vol_latest,
        "vol_avg_20": int(vol_avg_20) if isinstance(vol_avg_20, (int, float)) else 0,
        "precio_52w_high": round(precio_52w_high, 2) if precio_52w_high else None,
        "precio_52w_low": round(precio_52w_low, 2) if precio_52w_low else None,
        "tradingview": tv_result,
        "pe_ratio": fundamentos.get("pe_ratio"),
        "latest_close": round(latest_close, 2),
    }
    return datos


def pasar_filtro_liquidez(datos):
    """
    Filtro de liquidez: precio mínimo, volumen mínimo, market cap mínimo.
    """
    if not datos:
        return False

    precio = datos.get("precio", 0)
    if precio < 5:
        return False

    market_cap = datos.get("market_cap")
    if market_cap is not None and market_cap < 300_000_000:
        return False

    vol = datos.get("vol_latest", 0)
    if vol < 100_000:
        return False

    return True


# ═══════════════════════════════════════════════════════════════════
#  FASE 3: CLASIFICACIÓN ALCISTA/BAJISTA
# ═══════════════════════════════════════════════════════════════════

def analizar_senales_alcistas(datos):
    """Analiza señales alcistas y retorna (puntos, lista_señales)."""
    puntos = 0
    senales = []

    score_estimado = estimar_score(datos)
    if score_estimado >= 6.5:
        puntos += SIGNAL_WEIGHTS["score_alto"]
        senales.append("score_alto")

    if datos.get("sobre_sma50"):
        puntos += SIGNAL_WEIGHTS["sobre_sma50"]
        senales.append("sobre_sma50")

    if datos.get("sobre_sma200"):
        puntos += SIGNAL_WEIGHTS["sobre_sma200"]
        senales.append("sobre_sma200")

    rsi = datos.get("rsi_14")
    if rsi is not None and 40 <= rsi <= 58:
        puntos += SIGNAL_WEIGHTS["rsi_optimo"]
        senales.append("rsi_optimo")

    if datos.get("macd_bullish"):
        puntos += SIGNAL_WEIGHTS["macd_bullish"]
        senales.append("macd_bullish")

    if datos.get("golden_cross") is True:
        puntos += SIGNAL_WEIGHTS["golden_cross"]
        senales.append("golden_cross")

    if datos.get("volume_ratio", 0) > 1.2:
        puntos += SIGNAL_WEIGHTS["vol_alto"]
        senales.append("vol_alto")

    tv = datos.get("tradingview")
    if tv:
        rec = tv.get("recommendation", "")
        if rec in ("BUY", "STRONG_BUY"):
            puntos += SIGNAL_WEIGHTS["tradingview_bull"]
            senales.append("tradingview_bull")

    # Insider (si está disponible del enriquecimiento)
    if datos.get("insider_bullish"):
        puntos += SIGNAL_WEIGHTS["insider_buy"]
        senales.append("insider_buy")

    # Sector rotation (si está disponible)
    if datos.get("sector_modifier", 0) > 0.1:
        puntos += SIGNAL_WEIGHTS["sector_tailwind"]
        senales.append("sector_tailwind")

    # Analyst (si está disponible del enriquecimiento)
    if datos.get("analyst_bullish"):
        puntos += SIGNAL_WEIGHTS["analyst_bullish"]
        senales.append("analyst_bullish")

    return puntos, senales


def analizar_senales_bajistas(datos):
    """Analiza señales bajistas y retorna (puntos, lista_señales)."""
    puntos = 0
    senales = []

    score_estimado = estimar_score(datos)
    if score_estimado <= 4.5:
        puntos += SIGNAL_WEIGHTS["score_bajo"]
        senales.append("score_bajo")

    if datos.get("sobre_sma50") is False:
        puntos += SIGNAL_WEIGHTS["bajo_sma50"]
        senales.append("bajo_sma50")

    if datos.get("sobre_sma200") is False:
        puntos += SIGNAL_WEIGHTS["bajo_sma200"]
        senales.append("bajo_sma200")

    rsi = datos.get("rsi_14")
    if rsi is not None and rsi > 58:
        puntos += SIGNAL_WEIGHTS["rsi_sobrecomprado"]
        senales.append("rsi_sobrecomprado")

    if datos.get("macd_bullish") is False:
        puntos += SIGNAL_WEIGHTS["macd_bearish"]
        senales.append("macd_bearish")

    if datos.get("golden_cross") is False:
        puntos += SIGNAL_WEIGHTS["death_cross"]
        senales.append("death_cross")

    if datos.get("volume_ratio", 0) > 1.2:
        puntos += SIGNAL_WEIGHTS["vol_alto_bajista"]
        senales.append("vol_alto_bajista")

    tv = datos.get("tradingview")
    if tv:
        rec = tv.get("recommendation", "")
        if rec in ("SELL", "STRONG_SELL"):
            puntos += SIGNAL_WEIGHTS["tradingview_bear"]
            senales.append("tradingview_bear")

    if datos.get("insider_bearish"):
        puntos += SIGNAL_WEIGHTS["insider_sell"]
        senales.append("insider_sell")

    if datos.get("sector_modifier", 0) < -0.1:
        puntos += SIGNAL_WEIGHTS["sector_headwind"]
        senales.append("sector_headwind")

    if datos.get("analyst_bearish"):
        puntos += SIGNAL_WEIGHTS["analyst_bearish"]
        senales.append("analyst_bearish")

    return puntos, senales


def estimar_score(datos):
    """
    Estima un score compuesto 0-10 basado en datos técnicos.
    Similar a compute_quick_score pero sin depender de la librería.
    """
    score = 5.0

    if datos.get("sobre_sma200") is True:
        score += 1.5
    elif datos.get("sobre_sma200") is False:
        score -= 1.0

    if datos.get("sobre_sma50") is True:
        score += 1.0
    elif datos.get("sobre_sma50") is False:
        score -= 1.0

    rsi = datos.get("rsi_14")
    if rsi is not None:
        if rsi < 35:
            score += 1.5
        elif rsi < 45:
            score += 1.0
        elif rsi < 55:
            score += 0.5
        elif rsi > 70:
            score -= 1.0

    if datos.get("macd_bullish"):
        score += 1.0

    tv = datos.get("tradingview")
    if tv:
        rec = tv.get("recommendation", "")
        if rec == "STRONG_BUY":
            score += 2.0
        elif rec == "BUY":
            score += 1.0
        elif rec == "SELL":
            score -= 1.0
        elif rec == "STRONG_SELL":
            score -= 2.0

    return max(0.0, min(10.0, round(score, 2)))


def clasificar_candidato(datos):
    """
    Clasifica un candidato como ALCISTA, BAJISTA o None.
    Retorna dict con clasificación o None si es neutro.
    """
    puntos_alcista, senales_alcista = analizar_senales_alcistas(datos)
    puntos_bajista, senales_bajista = analizar_senales_bajistas(datos)

    # Determinar clasificación
    if puntos_alcista >= puntos_bajista and puntos_alcista >= MIN_BULLISH_THRESHOLD:
        nivel = "FUERTE" if puntos_alcista >= STRONG_THRESHOLD else "ALCISTA"
        return {
            "tipo": "ALCISTA",
            "nivel": nivel,
            "puntos": puntos_alcista,
            "max_posible": MAX_BULLISH_SCORE,
            "senales": senales_alcista,
            "senales_labels": [SIGNAL_LABELS[s] for s in senales_alcista],
        }
    elif puntos_bajista > puntos_alcista and puntos_bajista >= MIN_BEARISH_THRESHOLD:
        nivel = "FUERTE" if puntos_bajista >= STRONG_THRESHOLD else "BAJISTA"
        return {
            "tipo": "BAJISTA",
            "nivel": nivel,
            "puntos": puntos_bajista,
            "max_posible": MAX_BEARISH_SCORE,
            "senales": senales_bajista,
            "senales_labels": [SIGNAL_LABELS[s] for s in senales_bajista],
        }

    return None


# ═══════════════════════════════════════════════════════════════════
#  FASE 4: ENRIQUECIMIENTO CON FINNHUB
# ═══════════════════════════════════════════════════════════════════

def enriquecer_con_finnhub(candidato, config):
    """
    Añade datos de Finnhub: insider sentiment, analyst consensus, news.
    """
    ticker = candidato["ticker"]

    if not is_api_available("finnhub", config):
        return

    import requests
    key = None
    try:
        from scripts.api_config import get_api_key
        key = get_api_key("finnhub", config)
    except Exception:
        pass
    if not key:
        for entry in config.values():
            if isinstance(entry, dict) and isinstance(entry.get("key"), str) and len(entry["key"]) > 10:
                key = entry["key"]
                break
    if not key:
        return

    # Insider sentiment
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/insider-sentiment?symbol={ticker}&token={key}",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                latest = data[-1]
                mspr = latest.get("mspr", 0)
                if mspr > 0:
                    candidato["insider_bullish"] = True
                elif mspr < 0:
                    candidato["insider_bearish"] = True
                candidato["insider_mspr"] = mspr
                if "senales" in candidato:
                    pass  # actualizar después si hace falta
    except Exception:
        pass

    # Analyst ratings
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={key}",
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                latest = data[0]
                buy = latest.get("strongBuy", 0) + latest.get("buy", 0)
                sell = latest.get("strongSell", 0) + latest.get("sell", 0)
                total = buy + latest.get("hold", 0) + sell
                if total > 0:
                    buy_pct = buy / total
                    candidato["analyst_buy_pct"] = round(buy_pct * 100, 1)
                    if buy_pct > 0.5:
                        candidato["analyst_bullish"] = True
                    elif buy_pct < 0.3:
                        candidato["analyst_bearish"] = True
    except Exception:
        pass

    # News (últimas 3 noticias con sentimiento)
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={week_ago}&to={today}&token={key}",
            timeout=10,
        )
        if r.status_code == 200:
            articles = r.json()
            if articles:
                candidato["noticias"] = [
                    {"titulo": a.get("headline", "")[:100], "fuente": a.get("source", "")}
                    for a in articles[:3]
                ]
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
#  FASE 5: REPORTES
# ═══════════════════════════════════════════════════════════════════

def cleanup_reportes_viejos(max_dias=MAX_DAYS):
    """Mantiene solo los últimos N reportes."""
    if not os.path.exists(REPORTS_DIR):
        return
    reportes = sorted(glob.glob(os.path.join(REPORTS_DIR, "scan_*.json")))
    while len(reportes) > max_dias:
        os.remove(reportes.pop(0))


def guardar_reporte(resultado):
    """Guarda el resultado del screener como JSON en data/reports/."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    fecha = resultado.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"scan_{fecha}_{timestamp}.json"
    filepath = os.path.join(REPORTS_DIR, filename)

    # Make serializable (remove non-serializable fields)
    serializable = json.loads(json.dumps(resultado, default=str, ensure_ascii=False))

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)
    print(f"  Reporte guardado: {filepath}")
    return filepath


def listar_reportes():
    """Retorna lista de reportes disponibles ordenados por fecha."""
    if not os.path.exists(REPORTS_DIR):
        return []
    reportes = sorted(glob.glob(os.path.join(REPORTS_DIR, "scan_*.json")), reverse=True)
    resultado = []
    for r in reportes:
        try:
            with open(r, "r", encoding="utf-8") as f:
                data = json.load(f)
            basename = os.path.basename(r)
            fecha_str = basename.replace("scan_", "").replace(".json", "")
            resultado.append({
                "filename": basename,
                "path": r,
                "fecha": data.get("fecha", fecha_str[:10]),
                "scan_date": data.get("scan_date", ""),
                "alcistas_count": len(data.get("alcistas", [])),
                "bajistas_count": len(data.get("bajistas", [])),
                "candidatos": data.get("candidatos_brutos", 0),
            })
        except Exception:
            continue
    return resultado


def cargar_reporte_por_fecha(fecha):
    """Carga el reporte más reciente de una fecha específica."""
    if not os.path.exists(REPORTS_DIR):
        return None
    pattern = os.path.join(REPORTS_DIR, f"scan_{fecha}*.json")
    reportes = sorted(glob.glob(pattern), reverse=True)
    if not reportes:
        return None
    try:
        with open(reportes[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
#  PIPELINE PRINCIPAL
# ═══════════════════════════════════════════════════════════════════

def ejecutar_screener(tickers_especificos=None, verbose=True):
    """
    Ejecuta el pipeline completo del screener.

    Args:
        tickers_especificos: Lista opcional de tickers a analizar.
        verbose: Mostrar progreso en consola.

    Returns:
        dict con alcistas, bajistas, stats, etc.
    """
    def log(msg, end=None, flush=False):
        if verbose:
            kwargs = {}
            if end is not None:
                kwargs["end"] = end
            if flush:
                kwargs["flush"] = flush
            print(msg, **kwargs)

    config = load_config()
    start_time = time.time()

    log(f"\n{'='*60}")
    log(f"  STOCKS SCANNER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log(f"{'='*60}\n")

    # ─── Fase 1: Obtener candidatos ───────────────────────────
    fuentes = {}
    if tickers_especificos:
        log("Usando tickers específicos (sin escaneo de fuentes)")
        lista = [t.upper().strip() for t in tickers_especificos]
        candidatos_brutos = lista
        fuentes = {"manual": [(t, 1) for t in lista]}
    else:
        log("FASE 1: Escaneando fuentes...\n")
        rss = escanear_rss()
        if rss:
            fuentes["rss"] = rss

        reddit = escanear_reddit()
        if reddit:
            fuentes["reddit"] = reddit

        st = escanear_stocktwits()
        if st:
            fuentes["stocktwits"] = st

        if not fuentes:
            log("No se encontraron candidatos en ninguna fuente.")
            return None

        candidatos_brutos = fusionar_candidatos(fuentes)
        top_n = min(40, len(candidatos_brutos))
        log(f"\n  Top {top_n} candidatos por relevancia:\n")
        log(f"  {'Ticker':<8} {'Score':<8} {'Fuentes'}")
        log(f"  {'-'*35}")
        for ticker, menciones, nf, score, srcs in candidatos_brutos[:top_n]:
            log(f"  {ticker:<8} {score:<8.0f} {', '.join(srcs)}")

    # ─── Fase 2: Evaluar cada candidato ───────────────────────
    log(f"\nFASE 2: Evaluando candidatos...\n")

    candidatos = []
    if tickers_especificos:
        lista = candidatos_brutos
        fuente_info = {}
    else:
        lista = [t[0] for t in candidatos_brutos]
        fuente_info = {}
        for t, m, nf, sc, srcs in candidatos_brutos:
            fuente_info[t] = {"score": sc, "sources": srcs}

    for i, ticker in enumerate(lista):
        log(f"  [{i+1}/{len(lista)}] {ticker}...", end=" ", flush=True)

        datos = obtener_datos_candidato(ticker)
        if not datos:
            log("✗ sin datos")
            continue

        if not pasar_filtro_liquidez(datos):
            log(f"✗ liquidez (${datos.get('precio', 0)})")
            continue

        # Añadir info de fuentes
        if ticker in fuente_info:
            datos["fuente_score"] = fuente_info[ticker]["score"]
            datos["fuentes"] = fuente_info[ticker]["sources"]

        # Estimar score
        datos["score_estimado"] = estimar_score(datos)

        # Clasificar
        clasificacion = clasificar_candidato(datos)
        if clasificacion:
            datos["clasificacion"] = clasificacion
            puntos = clasificacion["puntos"]
            max_pts = clasificacion["max_posible"]
            log(f"✅ {clasificacion['tipo']} {clasificacion['nivel']} ({puntos}/{max_pts})")
            candidatos.append(datos)
        else:
            log(f"→ neutro ({datos['score_estimado']})")

        # Pequeña pausa entre tickers
        time.sleep(0.3)

    log(f"\n  Total evaluados: {len(lista)}")
    log(f"  Pasaron filtro: {len(candidatos)}")

    # ─── Fase 3: Separar alcistas y bajistas ──────────────────
    log(f"\nFASE 3: Clasificando...\n")

    alcistas = [c for c in candidatos if c.get("clasificacion", {}).get("tipo") == "ALCISTA"]
    bajistas = [c for c in candidatos if c.get("clasificacion", {}).get("tipo") == "BAJISTA"]

    # Ordenar por puntos de convicción
    alcistas.sort(key=lambda x: x["clasificacion"]["puntos"], reverse=True)
    bajistas.sort(key=lambda x: x["clasificacion"]["puntos"], reverse=True)

    top_alcistas = alcistas[:5]
    top_bajistas = bajistas[:5]

    log(f"  ALCISTAS: {len(alcistas)} total, top 5:")
    for c in top_alcistas:
        cl = c["clasificacion"]
        senales_str = ", ".join(cl["senales_labels"][:4])
        log(f"    {c['ticker']:<8} {c['score_estimado']}/10  "
            f"[{cl['nivel']}]  {cl['puntos']}/{cl['max_posible']}pts  "
            f"${c['precio']}  {senales_str}")

    log(f"\n  BAJISTAS: {len(bajistas)} total, top 5:")
    for c in top_bajistas:
        cl = c["clasificacion"]
        senales_str = ", ".join(cl["senales_labels"][:4])
        log(f"    {c['ticker']:<8} {c['score_estimado']}/10  "
            f"[{cl['nivel']}]  {cl['puntos']}/{cl['max_posible']}pts  "
            f"${c['precio']}  {senales_str}")

    # ─── Fase 4: Enriquecer con Finnhub ───────────────────────
    if top_alcistas or top_bajistas:
        log(f"\nFASE 4: Enriqueciendo con Finnhub...\n")
        for c in top_alcistas + top_bajistas:
            enriquecer_con_finnhub(c, config)
            log(f"  {c['ticker']}: insider={c.get('insider_mspr', 'N/A')} "
                f"analyst={c.get('analyst_buy_pct', 'N/A')}% buy")

    # ─── Fase 5: Preparar resultado ───────────────────────────
    fecha_actual = datetime.now().strftime("%Y-%m-%d")
    resultado = {
        "scan_date": datetime.now().isoformat(),
        "fecha": fecha_actual,
        "sources_scanned": list(fuentes.keys()) if fuentes else ["manual"],
        "candidatos_brutos": len(lista),
        "tras_liquidez": len(candidatos),
        "alcistas": _serializar_lista(top_alcistas),
        "bajistas": _serializar_lista(top_bajistas),
        "stats": {
            "total_alcistas": len(alcistas),
            "total_bajistas": len(bajistas),
            "neutros": len(candidatos) - len(alcistas) - len(bajistas),
            "sin_liquidez": len(lista) - len(candidatos),
            "elapsed_seconds": round(time.time() - start_time, 1),
        },
    }

    # Guardar reporte
    cleanup_reportes_viejos(MAX_DAYS)
    guardar_reporte(resultado)

    elapsed = round(time.time() - start_time, 1)
    log(f"\n{'='*60}")
    log(f"  SCAN COMPLETADO en {elapsed}s")
    log(f"  Candidatos: {len(lista)} → {len(candidatos)} tras filtro")
    log(f"  Alcistas: {len(alcistas)} (top 5 guardados)")
    log(f"  Bajistas: {len(bajistas)} (top 5 guardados)")
    log(f"{'='*60}\n")

    return resultado


def _serializar_lista(lista):
    """Convierte objetos candidato a dict serializable para JSON."""
    result = []
    for c in lista:
        item = {
            "ticker": c.get("ticker", ""),
            "name": c.get("name", ""),
            "precio": c.get("precio"),
            "score": c.get("score_estimado"),
            "rsi": c.get("rsi_14"),
            "macd_bullish": c.get("macd_bullish"),
            "sobre_sma50": c.get("sobre_sma50"),
            "sobre_sma200": c.get("sobre_sma200"),
            "golden_cross": c.get("golden_cross"),
            "volume_ratio": c.get("volume_ratio"),
            "market_cap": c.get("market_cap"),
            "sector": c.get("sector", ""),
            "tradingview_rec": c.get("tradingview", {}).get("recommendation") if c.get("tradingview") else None,
            "insider_mspr": c.get("insider_mspr"),
            "analyst_buy_pct": c.get("analyst_buy_pct"),
            "noticias": c.get("noticias", []),
        }

        cl = c.get("clasificacion", {})
        item["clasificacion"] = {
            "tipo": cl.get("tipo"),
            "nivel": cl.get("nivel"),
            "puntos": cl.get("puntos"),
            "max_posible": cl.get("max_posible"),
            "senales": cl.get("senales_labels", []),
        }

        result.append(item)
    return result


# ═══════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════

def print_resultado(resultado):
    """Imprime el resultado formateado en consola."""
    if not resultado:
        print("No hay resultados.")
        return

    print(f"\n{'='*70}")
    print(f"  SCAN: {resultado['fecha']}")
    print(f"  Fuentes: {', '.join(resultado['sources_scanned'])}")
    print(f"{'='*70}\n")

    print(f"  [ ALCISTAS ]\n")
    print(f"  {'Ticker':<10} {'Score':<7} {'Precio':<10} {'RSI':<7} {'MACD':<7} {'Señales':<40}")
    print(f"  {'-'*75}")
    for c in resultado.get("alcistas", []):
        cl = c.get("clasificacion", {})
        macd = "BULL" if c.get("macd_bullish") else "BEAR"
        senales = cl.get("senales", [])
        senales_trunc = ", ".join(senales[:3])
        if len(senales) > 3:
            senales_trunc += f" +{len(senales)-3}"
        nivel = cl.get("nivel", "")
        ticker_str = f"{c['ticker']} [{nivel}]" if nivel else c["ticker"]
        precio_str = f"${c['precio']:<7,.2f}" if c.get('precio') else "N/A"
        rsi_str = str(c['rsi']) if c.get('rsi') else "N/A"
        score_str = str(c['score']) if c.get('score') is not None else "N/A"
        print(f"  {ticker_str:<12} {score_str:<7} {precio_str:<10} {rsi_str:<7} {macd:<7} {senales_trunc:<40}")

    print(f"\n  [ BAJISTAS ]\n")
    print(f"  {'Ticker':<10} {'Score':<7} {'Precio':<10} {'RSI':<7} {'MACD':<7} {'Señales':<40}")
    print(f"  {'-'*75}")
    for c in resultado.get("bajistas", []):
        cl = c.get("clasificacion", {})
        macd = "BULL" if c.get("macd_bullish") else "BEAR"
        senales = cl.get("senales", [])
        senales_trunc = ", ".join(senales[:3])
        if len(senales) > 3:
            senales_trunc += f" +{len(senales)-3}"
        nivel = cl.get("nivel", "")
        ticker_str = f"{c['ticker']} [{nivel}]" if nivel else c["ticker"]
        precio_str = f"${c['precio']:<7,.2f}" if c.get('precio') else "N/A"
        rsi_str = str(c['rsi']) if c.get('rsi') else "N/A"
        score_str = str(c['score']) if c.get('score') is not None else "N/A"
        print(f"  {ticker_str:<12} {score_str:<7} {precio_str:<10} {rsi_str:<7} {macd:<7} {senales_trunc:<40}")

    stats = resultado.get("stats", {})
    print(f"\n  {'─'*70}")
    print(f"  Stats: {stats.get('total_alcistas', 0)} alcistas, "
          f"{stats.get('total_bajistas', 0)} bajistas, "
          f"{stats.get('neutros', 0)} neutros, "
          f"{stats.get('sin_liquidez', 0)} sin liquidez")
    print(f"  Tiempo: {stats.get('elapsed_seconds', 0)}s\n")


def main():
    parser = argparse.ArgumentParser(description="Bullish/Bearish Stock Screener")
    parser.add_argument("--tickers", "-t", nargs="+", help="Tickers específicos a analizar")
    parser.add_argument("--quiet", "-q", action="store_true", help="Output mínimo")
    parser.add_argument("--output", "-o", help="Guardar resultado en archivo")
    args = parser.parse_args()

    resultado = ejecutar_screener(
        tickers_especificos=args.tickers,
        verbose=not args.quiet,
    )

    if resultado:
        print_resultado(resultado)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(resultado, f, indent=2, ensure_ascii=False, default=str)
            print(f"Resultado guardado en: {args.output}")

        # Usage report
        try:
            tracker = get_tracker()
            tracker.print_daily_report()
        except Exception:
            pass


if __name__ == "__main__":
    main()
