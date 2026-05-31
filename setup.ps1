# Stocks Scanner Setup for Windows
Write-Host "=== Stocks Scanner Setup ===" -ForegroundColor Green

# Check Python
$py = "python"
try {
    $version = & $py --version
    Write-Host "Python: $version"
} catch {
    Write-Host "Python not found. Please install Python 3.10+" -ForegroundColor Red
    exit 1
}

# Create virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $py -m venv .venv
}

# Activate and install
.\.venv\Scripts\Activate.ps1

Write-Host "Installing dependencies..."
pip install -r requirements.txt

# Optional: pandas-ta
pip install pandas-ta 2>$null
if ($?) {
    Write-Host "pandas-ta installed" -ForegroundColor Green
} else {
    Write-Host "pandas-ta optional, using built-in TA" -ForegroundColor Yellow
}

Write-Host "Initializing configuration..."
python -c "from scripts.api_config import init_config; init_config()"

New-Item -ItemType Directory -Path "data/reports" -Force | Out-Null
New-Item -ItemType Directory -Path "data/.cache" -Force | Out-Null
New-Item -ItemType Directory -Path "logs" -Force | Out-Null

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host "Run: .venv\Scripts\Activate"
Write-Host "Then: python scripts/run_screener.py"
Write-Host "Or:   uvicorn api.main:app --reload"
