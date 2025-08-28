from __future__ import annotations

from fastapi import FastAPI

from .routes import routes


def create_app() -> FastAPI:
    app = FastAPI(title="PO Assistant", version="0.1.0")
    app.include_router(routes)
    return app


app = create_app()
