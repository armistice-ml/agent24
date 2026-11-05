import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.db import assert_safe_role, get_engine
from app.routers import auth, dashboard, events, webhooks
from app.services.events import EventHub


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level)
    assert_safe_role()
    app.state.hub = EventHub()
    listener = asyncio.create_task(app.state.hub.run())
    try:
        yield
    finally:
        listener.cancel()
        try:
            await listener
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Agent24", lifespan=lifespan)

_origin = get_settings().dashboard_origin
if _origin:  # le tableau de bord est servi depuis une autre origine : CORS strict, avec cookies
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

app.include_router(webhooks.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(events.router)


@app.get("/health")
def health():
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}
