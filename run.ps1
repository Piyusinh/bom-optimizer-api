# Start the BOM API locally using the bundled Python interpreter.
# This avoids the global `py` (Python 3.14) which cannot build the pinned packages.
$env:ENVIRONMENT = 'development'
.\.py-embed\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
