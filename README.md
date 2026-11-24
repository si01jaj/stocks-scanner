# Stocks Scanner

Escáner de acciones que detecta candidatos en fuentes públicas (RSS, Reddit,
StockTwits), los analiza técnicamente y publica una clasificación
**ALCISTA / BAJISTA / NEUTRO** con su desglose de señales, en un dashboard
FastAPI. Guarda cada señal en un histórico SQLite para poder medir después si
acierta.

> Proyecto de análisis, no asesoramiento financiero. La validación estadística
> de las señales está en construcción: hasta que haya histórico suficiente, las
> clasificaciones no deben usarse para operar.

---

## Arquitectura real

Hay un solo motor. Lo que no aparezca en este diagrama no se ejecuta:

```
fuentes (RSS · Reddit/ApeWisdom · StockTwits)
        │
        ├─ fusionar_candidatos()  →  ranking por menciones/diversidad
        │
        ├─ obtener_datos_candidato()   yfinance + tradingview-ta
        │     └─ technical_analysis._compute_rsi()  ← única implementación de RSI
        │
        ├─ pasar_filtro_liquidez()     precio · volumen · market cap
        │
        ├─ estimar_score()             score 0-10 (SMA50, SMA200, RSI, MACD, TV)
        ├─ analizar_senales_alcistas() / _bajistas()    puntos por señal
        ├─ clasificar_candidato()      score como filtro + margen mínimo
        │
        ├─ enriquecer_con_finnhub()    insider · analistas · noticias
        │
        ├─ guardar_reporte()           data/reports/scan_*.json
        └─ signals_ledger.record_scan()  data/signals.db
                                        │
                    api/main.py (FastAPI + Jinja) ── dashboard
                                        │
                    signals_ledger.update_outcomes() → hit-rate a 5/20/60 sesiones
```

### Reglas del modelo de puntuación

- **El score no puntúa como señal.** Se calcula con SMA50, SMA200, RSI, MACD y
  TradingView, que ya puntúan por separado; sumarlo otra vez era contar el
  mismo hecho tres veces. Su papel es filtrar el régimen (no se puede ser
  alcista con score bajo ni bajista con score alto).
- **El volumen exige dirección.** `vol_alto` sólo puntúa si la sesión subió;
  `vol_alto_bajista` sólo si bajó. Sin dato de variación no se concede el punto.
- **El RSI se penaliza una sola vez** (>65 resta 1,5 al score; >75 resta 3).
- **Un empate no se clasifica.** Se exige un margen mínimo de 1 punto entre
  bandos; si no, queda NEUTRO.
- **Los máximos se derivan de los pesos**, así que el denominador que muestra
  el dashboard no puede volver a desfasarse.

---

## Arranque rápido

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Un escaneo completo (fuentes públicas)
python scripts/run_screener.py

# Tickers concretos, sin escanear fuentes
python scripts/run_screener.py --tickers AAPL MSFT NVDA

# Dashboard
uvicorn api.main:app --port 8080
#  http://127.0.0.1:8080/
```

### Docker

```bash
cp config/api_keys.example.json config/api_keys.json   # añade tus claves
docker compose up -d --build
curl -s localhost:8030/api/status
```

El contenedor corre como usuario sin privilegios (`scanner`, uid 10001), tiene
healthcheck sobre `/api/status` y publica sólo en loopback: la exposición
externa va por cloudflared/traefik.

---

## API

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/scan/{fecha}` | Detalle de un escaneo |
| GET | `/ticker/{symbol}` | Histórico de un valor |
| GET | `/api/status` | Estado del sistema |
| GET | `/api/reports` | Lista de reportes |
| GET | `/api/reports/{fecha}` | Reporte completo |
| GET | `/api/ticker/{symbol}` | Histórico del ticker |
| GET | `/api/history` | Señales registradas + hit-rate por horizonte |
| POST | `/api/run` | **Lanza el scan en segundo plano** → `job_id` (202) |
| GET | `/api/run/{job_id}` | Estado del scan |

`POST /api/run` no bloquea: devuelve `{"job_id": ..., "status": "running"}` y se
consulta después. Si ya hay un escaneo en curso responde `409`. Si se define
`SCANNER_API_TOKEN`, exige la cabecera `X-API-Token`.

```bash
curl -s -X POST localhost:8030/api/run
curl -s localhost:8030/api/run/<job_id>
```

---

## Histórico y validación

Cada escaneo registra sus señales en `data/signals.db` (SQLite):

```bash
python scripts/signals_ledger.py init
python scripts/signals_ledger.py record data/reports/scan_2026-11-24_101500.json
python scripts/signals_ledger.py update    # rellena horizontes vencidos
python scripts/signals_ledger.py stats     # hit-rate, retorno medio, MFE/MAE
```

Horizontes: 5, 20 y 60 sesiones. La primera sesión contada es **posterior** al
día de la señal (no se puede comprar al cierre del día en que se publica) y se
guardan el máximo avance (MFE) y el máximo retroceso (MAE) del periodo.

`update` necesita red (yfinance) para traer los precios; el resto funciona
offline.

---

## Configuración

`config/api_keys.json` (no se versiona; ver `config/api_keys.example.json`):

```json
{ "finnhub": { "key": "TU_CLAVE", "enabled": true } }
```

- `enabled: false` desactiva la API aunque tenga clave.
- Si el fichero es JSON inválido se mueve a `api_keys.json.corrupt-<fecha>` y se
  regenera, en vez de sobrescribirlo a ciegas.

Variables de entorno:

| Variable | Efecto |
|---|---|
| `SCANNER_RETENTION_DAYS` | Días de reportes a conservar (por defecto 30) |
| `SCANNER_RETENTION_MAX` | Tope duro de ficheros de reporte (por defecto 500) |
| `SCANNER_DB_PATH` | Ruta del histórico SQLite (por defecto `data/signals.db`) |
| `SCANNER_API_TOKEN` | Token exigido en `POST /api/run` (vacío = sin token) |

---

## Tests

```bash
python -m pytest -q
```

Cubren: RSI (incluida la serie monótona que devolvía 50), máximos derivados de
los pesos, volumen con dirección, empate → neutro, RSI extremo sin invertir la
clasificación, `entry_exit` sin precios negativos, config corrupta sin
recursión, `enabled`, histórico (retornos/MFE/MAE) y los endpoints de la API.

---

## Limitaciones conocidas

- **El universo no es el mercado.** Los candidatos salen de fuentes sociales y
  RSS; es un escáner de valores *comentados*, no un screener de todo el mercado.
  `es_ticker_valido()` descarta símbolos con punto o guion (BRK.B, BRK-B).
- **La liquidez se mide en acciones, no en dólares** (volumen de la última
  sesión ≥ 100k), así que un valor de 2 $ pasa igual que uno de 500 $.
- **No hay backtest con costes ni deslizamiento**; el histórico mide retornos
  brutos a 5/20/60 sesiones.
- **`scripts/experimental/`** contiene módulos que no se ejecutan (scoring
  compuesto, proveedores alternativos, calendario macro, rotación sectorial).
  Ver su README antes de integrarlos.
- Los reportes JSON se siguen usando como fuente del dashboard; el histórico
  SQLite es la fuente para medir acierto.
