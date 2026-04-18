from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI, Depends
from fastapi.responses import JSONResponse

from .config import ensure_directories
from .auth import require_api_auth
from .db import Base, engine, migrate_schema
from .hooks import router as hooks_router
from .notifications import run_container_finalization_notifier
from .routes.containers import router as containers_router
from .routes.collections import router as collections_router
from .routes.progress import router as progress_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_directories()
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    notifier_task = asyncio.create_task(run_container_finalization_notifier())
    try:
        yield
    finally:
        notifier_task.cancel()
        with suppress(asyncio.CancelledError):
            await notifier_task


app = FastAPI(title="Riverhog", version="0.4.0", lifespan=lifespan)


@app.get("/healthz")
def healthcheck():
    return {"status": "ok"}


@app.exception_handler(ValueError)
async def value_error_handler(_request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(collections_router, dependencies=[Depends(require_api_auth)])
app.include_router(containers_router, dependencies=[Depends(require_api_auth)])
app.include_router(progress_router, dependencies=[Depends(require_api_auth)])
app.include_router(hooks_router)
