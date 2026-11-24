# Módulos en cuarentena

Estos módulos **no los importa nada** del proyecto. Se han movido aquí para que
la separación entre "lo que corre" y "lo que algún día correrá" sea visible, no
para tirarlos: tienen ideas útiles y `git mv` conserva su historia.

| Módulo | Qué hace | Estado |
|---|---|---|
| `scoring.py` | Scoring fundamental / técnico / sentimiento (40/30/30) con desglose por factor | Sin integrar. Necesita rediseño, no parches: normalización sectorial, FCF relativo al tamaño, ponderaciones que no cuenten como dato un 5,0 real, y `sentiment_data` que hoy se acepta y se ignora |
| `data_fetchers.py` | Proveedores alternativos (SEC EDGAR, FMP, Polygon, Finnhub, Alpha Vantage...) | Sin integrar. El fallback de `fundamentals` incluye SEC EDGAR, que devuelve *filings*, no indicadores |
| `data_cache.py` | Caché en disco de análisis y datos | Sin integrar |
| `sector_rotation.py` | Fuerza relativa por sector | Sin integrar |
| `macro_calendar.py` | Calendario macro (FOMC/CPI/NFP) + Finnhub | Sin integrar. Las fechas locales terminan en 2026-12-16 |

## Reglas para sacarlos de aquí

1. Un módulo entra en `scripts/` sólo si algún flujo real lo llama.
2. Antes de conectarlo, tests que cubran su cálculo (ver `tests/`).
3. Se borra el cálculo duplicado que ya exista en `run_screener.py` en el
   mismo PR de integración — no en otro posterior.

El motor que se ejecuta hoy es `scripts/run_screener.py` + `technical_analysis.py`
(indicadores) + `signals_ledger.py` (histórico).
