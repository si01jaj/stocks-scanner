FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional: pandas-ta for advanced TA (installed separately as it may fail on some arches)
RUN pip install --no-cache-dir pandas-ta 2>/dev/null || echo "pandas-ta optional, using built-in TA"

COPY . .

RUN python -c "from scripts.api_config import init_config; init_config()" 2>/dev/null || true

RUN mkdir -p data/reports data/.cache logs

EXPOSE 8080

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
