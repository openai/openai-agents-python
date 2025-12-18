import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Add the backend directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from models.database import init_db, AsyncSessionLocal
from services.auth import create_default_user
from routes import auth_router, chats_router, agents_router, plans_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    print("Starting Forestry MultiAgent System...")
    await init_db()
    print("Database initialized")

    # Create default user
    async with AsyncSessionLocal() as db:
        await create_default_user(db)

    yield

    # Shutdown
    print("Shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    description="A multiagent system for forestry operations management",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(auth_router, prefix="/api")
app.include_router(chats_router, prefix="/api")
app.include_router(agents_router, prefix="/api")
app.include_router(plans_router, prefix="/api")


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": "1.0.0"
    }


@app.get("/api/info")
async def app_info():
    """Application information."""
    return {
        "name": settings.APP_NAME,
        "description": "A multiagent system for forestry operations",
        "version": "1.0.0",
        "agents_count": 11,
        "features": [
            "Real-time chat with WebSocket support",
            "11 specialized forestry agents (A-K)",
            "Intelligent message routing",
            "Plan management and execution",
            "PostgreSQL persistence"
        ]
    }


# Serve static files for the frontend
static_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.exists(static_path):
    app.mount("/assets", StaticFiles(directory=os.path.join(static_path, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """Serve the React frontend."""
        # Check if the requested file exists
        file_path = os.path.join(static_path, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # Otherwise serve index.html for SPA routing
        return FileResponse(os.path.join(static_path, "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=settings.DEBUG
    )
