"""
Lingua Web — FastAPI application entry point.

Initialize the database on startup and mount all route handlers.
"""

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routes.upload import router as upload_router
from app.routes.study import router as study_router

app = FastAPI(
    title="Lingua Web",
    description="Japanese N2 language learning web prototype",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Startup: ensure database tables exist
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
app.include_router(upload_router)
app.include_router(study_router)


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------
from fastapi.responses import RedirectResponse

@app.get("/")
async def root():
    return RedirectResponse(url="/materials")


# ---------------------------------------------------------------------------
# Entry point (for direct execution)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
