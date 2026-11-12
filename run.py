"""Run the API server locally (default http://127.0.0.1:8001, the port the UI dev server proxies to)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8001")),
        reload=True,
        log_level="info",
    )
