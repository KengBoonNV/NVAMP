"""Local preview: the backend (in-memory store, local file dir) plus the frontend on one origin.

    cd substrait-app/backend
    NVAMP_DEV_USER=you@ninjavan.co NVAMP_OWNER_EMAILS=you@ninjavan.co \
        python ../tools/dev_server.py        # http://localhost:8000

Data lives in memory only and is lost on restart. Not used by the deployed app.
"""
import os
import sys
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from main import app  # noqa: E402

if os.getenv("NVAMP_DEV_FILES"):  # optional: serve a folder of test CSVs at /_devfiles
    app.mount("/_devfiles", StaticFiles(directory=os.environ["NVAMP_DEV_FILES"]), name="devfiles")
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "frontend", html=True), name="frontend")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
