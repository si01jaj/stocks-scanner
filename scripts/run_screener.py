#!/usr/bin/env python3
"""
Bullish/Bearish Stock Screener — Escanea, filtra, clasifica y reporta.

Pipeline:
  1. Escanear fuentes (RSS, Reddit, StockTwits) → lista de candidatos
  2. Quick-score cada candidato (yfinance + TradingView)
  3. Filtrar por liquidez (precio, volumen, market cap)
  4. Clasificar como ALCISTA / BAJISTA / NEUTRO según señales técnicas
  5. Enriquecer top 5 de cada categoría con Finnhub (insider, analyst)
  6. Guardar snapshot en data/reports/ (retención por días) y registrar las
     señales en el histórico SQLite (data/signals.db)

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
from scripts.api_config import load_config, is_api_available, get_api_key
from scripts.usage_tracker import get_tracker
from scripts.technical_analysis import _compute_rsi

REPORTS_DIR = os.path.join(_project_root, "data", "reports")
RETENTION_DAYS = int(os.environ.get("SCANNER_RETENTION_DAYS", "30"))
RETENTION_MAX_REPORTS = int(os.environ.get("SCANNER_RETENTION_MAX", "500"))

# ═══════════════════════════════════════════════════════════════════
#  PESOS PARA SEÑALES — cada señal suma puntos a la clasificación
# ═══════════════════════════════════════════════════════════════════
#
#  IMPORTANTE (invariante del diseño):
#  aquí sólo entran señales *independientes*. El score compuesto
#  (estimar_score) NO se puntúa como señal: se deriva de SMA50, SMA200,
#  RSI, MACD y TradingView, que ya puntúan por separado. Contarlo también
#  como puntos era una doble contabilización circular (el 100% de la
#  convicción de un valor podía venir del mismo hecho contado 3 veces).
#  Su papel ahora es el de filtro de régimen en clasificar_candidato().

SIGNAL_WEIGHTS = {
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

SIGNALS_ALCISTAS = (
    "sobre_sma50", "sobre_sma200", "rsi_optimo", "macd_bullish",
    "golden_cross", "vol_alto", "tradingview_bull", "insider_buy",
    "sector_tailwind", "analyst_bullish",
)
SIGNALS_BAJISTAS = (
    "bajo_sma50", "bajo_sma200", "rsi_sobrecomprado", "macd_bearish",
    "death_cross", "vol_alto_bajista", "tradingview_bear", "insider_sell",
    "sector_headwind", "analyst_bearish",
)

# El máximo se DERIVA de los pesos: nunca vuelve a quedarse desfasado.
MAX_BULLISH_SCORE = sum(SIGNAL_WEIGHTS[s] for s in SIGNALS_ALCISTAS)
MAX_BEARISH_SCORE = sum(SIGNAL_WEIGHTS[s] for s in SIGNALS_BAJISTAS)

MIN_BULLISH_THRESHOLD = 6
MIN_BEARISH_THRESHOLD = 6
STRONG_THRESHOLD = 9

# Nota de calibración: al retirar `score_alto`/`score_bajo` (3 puntos que eran
# el mismo hecho contado dos veces) la escala práctica bajó. Con los datos de
# un candidato típico sólo son evaluables ~10 de los 17 puntos posibles
# (TradingView, insider y analistas llegan en el enriquecimiento posterior),
# así que 6 puntos = estructura de tendencia (5) + una confirmación. El techo
# real de cada candidato se expone como `max_disponible`.

# Diferencia mínima entre bandos: un empate ya no se resuelve "a favor del
# alcista", se descarta. Exigir margen evita clasificaciones por moneda al aire.
MIN_SCORE_MARGIN = 1

# El score actúa como filtro de régimen, coherente con cada bando.
SCORE_GATE_BULLISH = 5.5
SCORE_GATE_BEARISH = 4.5

# Umbrales de RSI (una sola definición para todo el módulo)
RSI_OPTIMO_MAX = 45       # < 45: margen para subir (señal alcista)
RSI_OVERBOUGHT = 65       # > 65: sobrecompra (señal bajista)
RSI_EXTREME = 75          # > 75: sobrecompra extrema (penaliza el score)

VOLUME_RATIO_THRESHOLD = 1.2


SIGNAL_LABELS = {
    "sobre_sma50": "Precio > SMA50",
    "bajo_sma50": "Precio < SMA50",
    "sobre_sma200": "Precio > SMA200",
    "bajo_sma200": "Precio < SMA200",
    "rsi_optimo": f"RSI < {RSI_OPTIMO_MAX} (room to run)",
    "rsi_sobrecomprado": f"RSI > {RSI_OVERBOUGHT} (sobrecomprado)",
    "macd_bullish": "MACD alcista",
    "macd_bearish": "MACD bajista",
    "golden_cross": "Golden cross",
    "death_cross": "Death cross",
    "vol_alto": "Volumen alto en subida",
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
    if ticker in COMMON_WORDS or ticker in _CRYPTO_TICKERS:
        return False
    return True


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

    # RSI — implementación ÚNICA (scripts/technical_analysis._compute_rsi).
    # Antes había una copia local que devolvía 50 cuando no había pérdidas en
    # 14 sesiones, es decir justo en los valores parabólicos (donde el aviso de
    # sobrecompra importa). La implementación buena ya existía en el repo.
    rsi = _compute_rsi(close, 14)
    if rsi is None:  # serie plana o historia insuficiente
        rsi = 50.0

    # Variación de la última sesión: necesaria para saber si el volumen alto
    # fue de subida o de caída.
    if len(close) >= 2 and float(close.iloc[-2]) > 0:
        variacion_1d = (latest_close / float(close.iloc[-2]) - 1) * 100
    else:
        variacion_1d = 0.0

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
        "variacion_pct_1d": round(variacion_1d, 2),
        "vol_latest": vol_latest,
        "vol_avg_20": int(vol_avg_20) if isinstance(vol_avg_20, (int, float)) else 0,
        "precio_52w_high": round(precio_52w_high, 2) if precio_52w_high else None,
        "precio_52w_low": round(precio_52w_low, 2) if precio_52w_low else None,
        "tradingview": tv_result,
        "pe_ratio": fundamentos.get("pe_ratio"),
        "latest_close": round(latest_close, 2),
    }
    return datos


_CRYPTO_TICKERS = {
    "BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "DOT", "AVAX", "MATIC",
    "LINK", "UNI", "SHIB", "LTC", "BCH", "ATOM", "XLM", "NEAR", "ALGO",
    "FTM", "HBAR", "VET", "MANA", "SAND", "AXS", "THETA", "FIL", "EOS",
    "AAVE", "GRT", "XTZ", "FLOW", "CHZ", "ENJ", "CRV", "COMP", "YFI",
    "SUSHI", "BAT", "ZEC", "DASH", "XMR", "IOTA", "KLAY", "ONE",
    "CELO", "ANKR", "STORJ", "LRC", "IMX", "APE", "OP", "ARB",
    "SUI", "SEI", "TIA", "JUP", "WIF", "BONK", "PEPE", "FLOKI",
    "PENGU", "TOSHI", "DOG", "LUNC", "JASMY", "GALA", "RENDER",
    "BITO", "BITQ", "BLOK", "ETHE", "GBTC",
}


def es_crypto_o_etf_crypto(ticker, datos=None):
    """Detecta si un ticker es crypto o ETF de crypto."""
    t = ticker.upper()
    if t in _CRYPTO_TICKERS:
        return True
    if datos:
        sector = (datos.get("sector") or "").lower()
        name = (datos.get("name") or "").lower()
        industry = (datos.get("industry") or "").lower()
        crypto_keywords = ["crypto", "bitcoin", "ethereum", "digital asset", "blockchain"]
        for kw in crypto_keywords:
            if kw in sector or kw in industry or kw in name:
                return True
    return False


def pasar_filtro_liquidez(datos):
    """
    Filtro de liquidez: precio mínimo, volumen mínimo, market cap mínimo.
    """
    if not datos:
        return False

    if es_crypto_o_etf_crypto(datos.get("ticker", ""), datos):
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

    if datos.get("sobre_sma50"):
        puntos += SIGNAL_WEIGHTS["sobre_sma50"]
        senales.append("sobre_sma50")

    if datos.get("sobre_sma200"):
        puntos += SIGNAL_WEIGHTS["sobre_sma200"]
        senales.append("sobre_sma200")

    rsi = datos.get("rsi_14")
    if rsi is not None and rsi < RSI_OPTIMO_MAX:
        puntos += SIGNAL_WEIGHTS["rsi_optimo"]
        senales.append("rsi_optimo")

    if datos.get("macd_bullish"):
        puntos += SIGNAL_WEIGHTS["macd_bullish"]
        senales.append("macd_bullish")

    if datos.get("golden_cross") is True:
        puntos += SIGNAL_WEIGHTS["golden_cross"]
        senales.append("golden_cross")

    # El volumen sólo es señal alcista si la sesión fue de subida.
    if _volumen_alto_con_direccion(datos, alcista=True):
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


def _volumen_alto_con_direccion(datos, alcista):
    """
    Volumen por encima de lo normal Y sesión en la dirección esperada.

    El motor anterior sumaba +1 al bando bajista ("Volumen alto en caída")
    con sólo mirar el ratio, sin comprobar que el precio hubiera caído: el
    mismo dato puntuaba en los dos bandos. Si no hay variación disponible no
    se concede el punto (mejor ausencia de señal que señal inventada).
    """
    if datos.get("volume_ratio", 0) <= VOLUME_RATIO_THRESHOLD:
        return False
    var = datos.get("variacion_pct_1d")
    if var is None:
        return False
    return var >= 0 if alcista else var < 0


def analizar_senales_bajistas(datos):
    """Analiza señales bajistas y retorna (puntos, lista_señales)."""
    puntos = 0
    senales = []

    if datos.get("sobre_sma50") is False:
        puntos += SIGNAL_WEIGHTS["bajo_sma50"]
        senales.append("bajo_sma50")

    if datos.get("sobre_sma200") is False:
        puntos += SIGNAL_WEIGHTS["bajo_sma200"]
        senales.append("bajo_sma200")

    rsi = datos.get("rsi_14")
    if rsi is not None and rsi > RSI_OVERBOUGHT:
        puntos += SIGNAL_WEIGHTS["rsi_sobrecomprado"]
        senales.append("rsi_sobrecomprado")

    if datos.get("macd_bullish") is False:
        puntos += SIGNAL_WEIGHTS["macd_bearish"]
        senales.append("macd_bearish")

    if datos.get("golden_cross") is False:
        puntos += SIGNAL_WEIGHTS["death_cross"]
        senales.append("death_cross")

    # El volumen sólo es señal bajista si la sesión fue de caída.
    if _volumen_alto_con_direccion(datos, alcista=False):
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
        # Escala ÚNICA de RSI: antes se restaba -2 por rsi>65 y otros -2 por
        # rsi>75 (acumulativo), más -3 adicionales en las señales. Un valor
        # con estructura alcista perfecta y RSI 80 acababa con score 4.5 y
        # clasificado como bajista. Ahora la sobrecompra penaliza una vez.
        if rsi < 35:
            score += 1.5
        elif rsi < RSI_OPTIMO_MAX:
            score += 1.0
        elif rsi < 55:
            score += 0.5
        elif rsi > RSI_EXTREME:
            score -= 3.0
        elif rsi > RSI_OVERBOUGHT:
            score -= 1.5

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


def _senal_disponible(nombre, datos):
    """
    ¿Se pudo evaluar realmente esta señal con los datos del candidato?

    Sirve para no exigir 8 puntos cuando el candidato sólo permite evaluar 10:
    TradingView, insider y analistas llegan después de clasificar, y sin este
    dato el denominador del dashboard engañaba.
    """
    if nombre in ("sobre_sma50", "bajo_sma50"):
        return datos.get("sobre_sma50") is not None
    if nombre in ("sobre_sma200", "bajo_sma200"):
        return datos.get("sobre_sma200") is not None
    if nombre in ("rsi_optimo", "rsi_sobrecomprado"):
        return datos.get("rsi_14") is not None
    if nombre in ("macd_bullish", "macd_bearish"):
        return datos.get("macd_bullish") is not None
    if nombre in ("golden_cross", "death_cross"):
        return datos.get("golden_cross") is not None
    if nombre in ("vol_alto", "vol_alto_bajista"):
        return datos.get("variacion_pct_1d") is not None
    if nombre in ("tradingview_bull", "tradingview_bear"):
        return bool(datos.get("tradingview"))
    if nombre in ("insider_buy", "insider_sell"):
        return datos.get("insider_mspr") is not None
    if nombre in ("analyst_bullish", "analyst_bearish"):
        return datos.get("analyst_buy_pct") is not None
    if nombre in ("sector_tailwind", "sector_headwind"):
        return datos.get("sector_modifier") is not None
    return False


def max_puntos_disponibles(datos, alcista=True):
    """Techo evaluable para este candidato (no el máximo teórico del modelo)."""
    nombres = SIGNALS_ALCISTAS if alcista else SIGNALS_BAJISTAS
    return sum(SIGNAL_WEIGHTS[s] for s in nombres if _senal_disponible(s, datos))


def clasificar_candidato(datos):
    """
    Clasifica un candidato como ALCISTA, BAJISTA o None (neutro).

    Reglas:
      - El score compuesto actúa como FILTRO de régimen (no como puntos): no
        se puede ser alcista con score bajo ni bajista con score alto.
      - Se exige un margen mínimo entre bandos: el empate ya no se resuelve
        a favor del alcista, se descarta.
    """
    puntos_alcista, senales_alcista = analizar_senales_alcistas(datos)
    puntos_bajista, senales_bajista = analizar_senales_bajistas(datos)
    score = datos.get("score_estimado", 5.0)

    alcista_valido = (
        puntos_alcista >= MIN_BULLISH_THRESHOLD
        and score >= SCORE_GATE_BULLISH
        and (puntos_alcista - puntos_bajista) >= MIN_SCORE_MARGIN
    )
    bajista_valido = (
        puntos_bajista >= MIN_BEARISH_THRESHOLD
        and score <= SCORE_GATE_BEARISH
        and (puntos_bajista - puntos_alcista) >= MIN_SCORE_MARGIN
    )

    if alcista_valido:
        nivel = "FUERTE" if puntos_alcista >= STRONG_THRESHOLD else "ALCISTA"
        return {
            "tipo": "ALCISTA",
            "nivel": nivel,
            "puntos": puntos_alcista,
            "max_posible": MAX_BULLISH_SCORE,
            "max_disponible": max_puntos_disponibles(datos, alcista=True),
            "score": score,
            "margen": puntos_alcista - puntos_bajista,
            "senales": senales_alcista,
            "senales_labels": [SIGNAL_LABELS.get(s, s) for s in senales_alcista],
        }

    if bajista_valido:
        nivel = "FUERTE" if puntos_bajista >= STRONG_THRESHOLD else "BAJISTA"
        return {
            "tipo": "BAJISTA",
            "nivel": nivel,
            "puntos": puntos_bajista,
            "max_posible": MAX_BEARISH_SCORE,
            "max_disponible": max_puntos_disponibles(datos, alcista=False),
            "score": score,
            "margen": puntos_bajista - puntos_alcista,
            "senales": senales_bajista,
            "senales_labels": [SIGNAL_LABELS.get(s, s) for s in senales_bajista],
        }

    return None


def motivo_descarte(datos, puntos_alcista=None, puntos_bajista=None):
    """Motivo legible del descarte, para trazabilidad del scan."""
    pb = puntos_alcista if puntos_alcista is not None else analizar_senales_alcistas(datos)[0]
    pr = puntos_bajista if puntos_bajista is not None else analizar_senales_bajistas(datos)[0]
    score = datos.get("score_estimado", 5.0)
    if pb < MIN_BULLISH_THRESHOLD and pr < MIN_BEARISH_THRESHOLD:
        return "señales insuficientes"
    if abs(pb - pr) < MIN_SCORE_MARGIN:
        return f"empate técnico ({pb}-{pr})"
    if pb > pr and score < SCORE_GATE_BULLISH:
        return f"score {score} por debajo del umbral alcista"
    if pr > pb and score > SCORE_GATE_BEARISH:
        return f"score {score} por encima del umbral bajista"
    return "sin clasificación"


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
    # Sólo la clave de Finnhub. Antes, si faltaba, se recorría el resto del
    # fichero de config y se enviaba la primera clave de +10 caracteres a
    # finnhub.io: podía exfiltrar la clave de Alpha Vantage o Polygon.
    key = get_api_key("finnhub", config)
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

def cleanup_reportes_viejos(max_dias=None, max_reportes=None):
    """
    Elimina reportes con más antigüedad que `max_dias` (por fecha del nombre)
    y aplica un tope de seguridad de `max_reportes`.

    El nombre antiguo (`MAX_DAYS`) y el docstring decían "5 días" pero el
    código conservaba los 5 ficheros más recientes, que en la práctica son
    cinco escaneos: se perdía el histórico en días de mucho uso y se
    conservaban meses de reportes viejos si el scanner no corría.
    """
    if not os.path.exists(REPORTS_DIR):
        return 0
    max_dias = RETENTION_DAYS if max_dias is None else max_dias
    max_reportes = RETENTION_MAX_REPORTS if max_reportes is None else max_reportes

    limite = date.today() - timedelta(days=max_dias)
    reportes = sorted(glob.glob(os.path.join(REPORTS_DIR, "scan_*.json")))
    eliminados = 0

    for path in reportes:
        basename = os.path.basename(path)
        try:
            fecha_reporte = datetime.strptime(basename[5:15], "%Y-%m-%d").date()
        except ValueError:
            fecha_reporte = date.fromtimestamp(os.path.getmtime(path))
        if fecha_reporte < limite:
            try:
                os.remove(path)
                eliminados += 1
            except OSError:
                pass

    # Tope duro por número, por si algo genera muchos reportes en un mismo día
    restantes = sorted(glob.glob(os.path.join(REPORTS_DIR, "scan_*.json")))
    for path in restantes[:-max_reportes] if len(restantes) > max_reportes else []:
        try:
            os.remove(path)
            eliminados += 1
        except OSError:
            pass

    return eliminados


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
    contadores = {"sin_datos": 0, "sin_liquidez": 0, "neutros": 0}
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
            contadores["sin_datos"] += 1
            log("✗ sin datos")
            continue

        if not pasar_filtro_liquidez(datos):
            contadores["sin_liquidez"] += 1
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
            disp = clasificacion["max_disponible"]
            log(f"✅ {clasificacion['tipo']} {clasificacion['nivel']} ({puntos}/{disp} disponibles)")
            candidatos.append(datos)
        else:
            contadores["neutros"] += 1
            log(f"→ neutro ({datos['score_estimado']}) — {motivo_descarte(datos)}")

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
            f"[{cl['nivel']}]  {cl['puntos']}/{cl.get('max_disponible', cl['max_posible'])}pts  "
            f"${c['precio']}  {senales_str}")

    log(f"\n  BAJISTAS: {len(bajistas)} total, top 5:")
    for c in top_bajistas:
        cl = c["clasificacion"]
        senales_str = ", ".join(cl["senales_labels"][:4])
        log(f"    {c['ticker']:<8} {c['score_estimado']}/10  "
            f"[{cl['nivel']}]  {cl['puntos']}/{cl.get('max_disponible', cl['max_posible'])}pts  "
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
            "neutros": contadores["neutros"],
            "sin_datos": contadores["sin_datos"],
            "sin_liquidez": contadores["sin_liquidez"],
            "elapsed_seconds": round(time.time() - start_time, 1),
        },
    }

    # Guardar reporte + histórico persistente
    try:
        from scripts.signals_ledger import record_scan
        n_registradas = record_scan(resultado)
        log(f"  Histórico: {n_registradas} señales registradas")
    except Exception as e:  # el ledger nunca debe romper un scan
        log(f"  Histórico: no se pudo registrar ({e})")

    eliminados = cleanup_reportes_viejos()
    if eliminados:
        log(f"  Reportes antiguos eliminados: {eliminados}")
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
            "variacion_pct_1d": c.get("variacion_pct_1d"),
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
            "max_disponible": cl.get("max_disponible"),
            "margen": cl.get("margen"),
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
