FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Opcional: pandas-ta para indicadores avanzados. technical_analysis.py
# funciona sin él (implementación propia de RSI / ATR / MACD).
RUN pip install --no-cache-dir pandas-ta 2>/dev/null || echo "pandas-ta optional, using built-in TA"

COPY . .

# Directorios de datos + config de ejemplo + usuario sin privilegios
RUN mkdir -p data/reports data/.cache logs \
    && python -c "from scripts.api_config import init_config; init_config()" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin scanner \
    && chown -R scanner:scanner /app

# El contenedor no corre como root: el proceso sólo necesita escribir data/ y logs/
USER scanner

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=3); sys.exit(0 if r.status == 200 else 1)"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
