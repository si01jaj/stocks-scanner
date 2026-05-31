"""
API Configuration Manager for Financial Analysis Skill.
Handles API key storage, validation, free/paid tier tracking, and rate limit definitions.

Usage:
    from scripts.api_config import load_config, get_api_key, get_rate_limit, list_apis
"""
import json, os, sys
from pathlib import Path

_project_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_CONFIG_PATH = os.path.join(_project_root, "config", "api_keys.json")
LOGS_DIR = os.path.join(_project_root, "logs")

# ─── API REGISTRY ────────────────────────────────────────────
# Every API the skill uses, with tier/cost/limit metadata
API_REGISTRY = {
    "yfinance": {
        "name": "Yahoo Finance (yfinance)",
        "tier": "FREE",
        "cost": "Free (unofficial)",
        "requires_key": False,
        "rate_limit_per_minute": 5,       # self-imposed (avoid IP blocks)
        "rate_limit_per_day": 2000,       # self-imposed
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 2.0,
        "notes": "Unofficial scraper. Use batch requests + delays. No API key needed.",
    },
    "finnhub": {
        "name": "Finnhub",
        "tier": "FREE",
        "cost": "Free (60/min), paid $50/mo",
        "requires_key": True,
        "key_env_var": "FINNHUB_API_KEY",
        "key_url": "https://finnhub.io/register",
        "rate_limit_per_minute": 60,
        "rate_limit_per_day": 86400,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 1.0,
        "notes": "Free signup. 60 calls/min on free tier.",
    },
    "sec_edgar": {
        "name": "SEC EDGAR (Official)",
        "tier": "FREE",
        "cost": "Free (government)",
        "requires_key": False,
        "user_agent_required": True,
        "rate_limit_per_minute": 600,     # 10/sec
        "rate_limit_per_day": 864000,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 0.1,
        "notes": "Public data. Set User-Agent email in config.",
    },
    "mboum": {
        "name": "Mboum Finance",
        "tier": "FREE",
        "cost": "Free (600/mo), Pro $9.95/mo",
        "requires_key": True,
        "key_env_var": "MBOUM_API_KEY",
        "key_url": "https://mboum.com/",
        "rate_limit_per_minute": 60,
        "rate_limit_per_day": 20,
        "rate_limit_per_month": 600,
        "delay_between_calls_sec": 1.0,
        "notes": "Congress trading, options flow. 600 req/month free.",
    },
    "alpha_vantage": {
        "name": "Alpha Vantage",
        "tier": "FREE",
        "cost": "Free (25/day), paid $29.99/mo",
        "requires_key": True,
        "key_env_var": "ALPHA_VANTAGE_API_KEY",
        "key_url": "https://www.alphavantage.co/support/#api-key",
        "rate_limit_per_minute": 5,
        "rate_limit_per_day": 25,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 12.0,
        "notes": "News sentiment (AI-scored), technical indicators. 5/min, 25/day.",
    },
    "seeking_alpha_rapidapi": {
        "name": "Seeking Alpha (RapidAPI)",
        "tier": "PAID",
        "cost": "Paid via RapidAPI (~$0.01/call)",
        "requires_key": True,
        "key_env_var": "SEEKING_ALPHA_RAPIDAPI_KEY",
        "key_url": "https://rapidapi.com/apidojo/api/seeking-alpha/",
        "rate_limit_per_minute": 60,
        "rate_limit_per_day": 1000,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 1.0,
        "notes": "SA quant ratings + factor grades. Paid RapidAPI.",
    },
    "polygon": {
        "name": "Polygon.io",
        "tier": "FREE",
        "cost": "Free (5 calls/min), paid $29/mo",
        "requires_key": True,
        "key_env_var": "POLYGON_API_KEY",
        "key_url": "https://polygon.io/dashboard/signup",
        "rate_limit_per_minute": 5,
        "rate_limit_per_day": 5000,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 5.0,
        "notes": "Price history, technicals. 5 API calls/min free.",
    },
    "alpaca": {
        "name": "Alpaca Markets",
        "tier": "FREE",
        "cost": "Free (300 req/min), paid from $9.99/mo",
        "requires_key": True,
        "key_env_var": "ALPACA_API_KEY",
        "key_url": "https://alpaca.markets/",
        "rate_limit_per_minute": 300,
        "rate_limit_per_day": None,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 0.2,
        "notes": "News API (free). 300 requests/min.",
    },
    "fmp": {
        "name": "Financial Modeling Prep",
        "tier": "FREE",
        "cost": "Free (250/day), paid $19/mo",
        "requires_key": True,
        "key_env_var": "FMP_API_KEY",
        "key_url": "https://financialmodelingprep.com/",
        "rate_limit_per_minute": 60,
        "rate_limit_per_day": 250,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 2.0,
        "notes": "Company profiles, financials, price history.",
    },
    "apewisdom": {
        "name": "ApeWisdom",
        "tier": "FREE",
        "cost": "Free (unofficial)",
        "requires_key": False,
        "rate_limit_per_minute": 10,
        "rate_limit_per_day": 100,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 1.0,
        "notes": "Reddit trending stocks. No API key needed.",
    },
    "stocktwits": {
        "name": "StockTwits",
        "tier": "FREE",
        "cost": "Free (unofficial)",
        "requires_key": False,
        "rate_limit_per_minute": 10,
        "rate_limit_per_day": 200,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 1.0,
        "notes": "Social sentiment. No API key needed (unofficial endpoints).",
    },
    "tradingview": {
        "name": "TradingView (tradingview-ta)",
        "tier": "FREE",
        "cost": "Free (Python library)",
        "requires_key": False,
        "rate_limit_per_minute": 30,
        "rate_limit_per_day": 500,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 0.5,
        "notes": "Technical analysis consensus from 26 indicators. pip install tradingview-ta.",
    },
    "quiver": {
        "name": "Quiver Quantitative",
        "tier": "PAID",
        "cost": "Paid (starts at $29/mo)",
        "requires_key": True,
        "key_env_var": "QUIVER_API_KEY",
        "key_url": "https://www.quiverquant.com/",
        "rate_limit_per_minute": 60,
        "rate_limit_per_day": None,
        "rate_limit_per_month": None,
        "delay_between_calls_sec": 1.0,
        "notes": "Congress trading, insider trading, government contracts.",
    },
}

# ─── FALLBACK CHAINS ──────────────────────────────────────────
# For each data category, ordered list of APIs to try.
# If the primary fails or is rate-limited, the next is tried.
FALLBACK_CHAINS = {
    "price_history":        ["yfinance", "polygon", "alpha_vantage", "fmp"],
    "fundamentals":         ["yfinance", "sec_edgar", "finnhub", "fmp"],
    "analyst_ratings":      ["finnhub", "yfinance", "seeking_alpha_rapidapi"],
    "insider_trades":       ["sec_edgar", "finnhub"],
    "insider_sentiment":    ["finnhub"],
    "congress_trades":      ["mboum", "quiver"],
    "news_sentiment":       ["finnhub", "alpha_vantage", "alpaca"],
    "reddit_sentiment":     ["apewisdom"],
    "social_sentiment":     ["stocktwits"],
    "earnings":             ["finnhub", "yfinance"],
    "dividends":            ["yfinance"],
    "tradingview":          ["tradingview"],
}

SEC_EDGAR_USER_AGENT = "StocksScanner/1.0 contact@example.com"


# ─── PUBLIC API ──────────────────────────────────────────────

def ensure_dirs():
    """Create necessary directories."""
    os.makedirs(os.path.dirname(DEFAULT_CONFIG_PATH), exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)


def init_config():
    """Initialize a config file with template placeholders. Does NOT overwrite existing."""
    ensure_dirs()
    if os.path.exists(DEFAULT_CONFIG_PATH):
        print(f"Config already exists: {DEFAULT_CONFIG_PATH}")
        return load_config()
    config = {}
    for api_id, meta in API_REGISTRY.items():
        config[api_id] = {"key": None, "enabled": not meta.get("requires_key", False)}
    os.makedirs(os.path.dirname(DEFAULT_CONFIG_PATH), exist_ok=True)
    with open(DEFAULT_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Config initialized: {DEFAULT_CONFIG_PATH}")
    print("Edit the file to add your API keys.")
    return config


def load_config():
    """Load API config from disk. Creates default if missing."""
    if not os.path.exists(DEFAULT_CONFIG_PATH):
        return init_config()
    try:
        with open(DEFAULT_CONFIG_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return init_config()


def save_config(config):
    """Save API config to disk."""
    os.makedirs(os.path.dirname(DEFAULT_CONFIG_PATH), exist_ok=True)
    with open(DEFAULT_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_api_key(api_id, config=None):
    """Get an API key from config or environment variable."""
    if config is None:
        config = load_config()
    entry = config.get(api_id, {})
    key = entry.get("key") if isinstance(entry, dict) else None
    if key:
        return key
    env_var = API_REGISTRY.get(api_id, {}).get("key_env_var", "")
    if env_var:
        return os.environ.get(env_var, "")
    return None


def is_api_available(api_id, config=None):
    """Check if an API is available (key present or no key needed)."""
    if config is None:
        config = load_config()
    meta = API_REGISTRY.get(api_id, {})
    if not meta.get("requires_key", True):
        return True
    key = get_api_key(api_id, config)
    return bool(key and key.strip())


def get_rate_limit(api_id, period="per_minute"):
    """Get rate limit for an API. period: per_minute, per_day, per_month."""
    meta = API_REGISTRY.get(api_id, {})
    key = f"rate_limit_{period}"
    return meta.get(key, None)


def get_fallback_chain(data_category, config=None):
    """Get the fallback chain for a data category, filtering unavailable APIs."""
    chain = FALLBACK_CHAINS.get(data_category, [])
    if config is None:
        return chain
    return [api_id for api_id in chain if is_api_available(api_id, config)]


def list_apis():
    """Print a formatted table of all registered APIs with their status."""
    config = load_config()
    print(f"{'API ID':<28} {'Tier':<8} {'Cost':<30} {'Key':<8} {'Rate Limit'}")
    print("-" * 110)
    for api_id, meta in sorted(API_REGISTRY.items()):
        tier = meta.get("tier", "?")
        cost = meta.get("cost", "?")
        has_key = "✓" if is_api_available(api_id, config) else "—"
        rl = meta.get("rate_limit_per_day", "—")
        rl_str = f"{rl}/day" if rl else "—"
        print(f"{api_id:<28} {tier:<8} {cost:<30} {has_key:<8} {rl_str}")


def status():
    """Print formatted status report."""
    list_apis()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "init":
            init_config()
        elif cmd == "status":
            status()
        elif cmd == "list":
            list_apis()
        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python scripts/api_config.py [init|status|list]")
    else:
        status()
