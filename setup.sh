#!/bin/bash
set -e

echo "=== Stocks Scanner Setup ==="

# Check Python version
PYTHON="${PYTHON:-python3}"
if command -v "$PYTHON" &>/dev/null; then
    echo "Python: $($PYTHON --version)"
else
    echo "Python not found. Please install Python 3.10+"
    exit 1
fi

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
fi

source .venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Optional: pandas-ta
pip install pandas-ta 2>/dev/null || echo "pandas-ta optional, skipping"

# Init config
echo "Initializing configuration..."
$PYTHON -c "from scripts.api_config import init_config; init_config()"

mkdir -p data/reports data/.cache logs

echo ""
echo "=== Setup Complete ==="
echo "Run: source .venv/bin/activate"
echo "Then: python scripts/run_screener.py"
echo "Or:   uvicorn api.main:app --reload"
