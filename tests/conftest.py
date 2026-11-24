"""Configuración de pytest: rutas del proyecto y aislamiento de la BD."""
import os
import sys
import tempfile

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# El ledger nunca debe escribir en el data/ real durante los tests
os.environ.setdefault(
    "SCANNER_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="scanner-tests-"), "signals.db")
)
