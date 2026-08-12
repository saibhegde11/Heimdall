"""
Heimdall — Enhanced Mock Backend Server

A richer backend server used for testing the load balancer.
Provides multiple endpoints to exercise different proxying capabilities:

  GET  /           → Server info
  GET  /health     → Health check endpoint (for the health checker)
  POST /echo       → Echoes the request body back (tests POST proxying)
  GET  /slow       → Artificial delay (tests timeout handling)
  GET  /items/{id} → Path parameter endpoint (tests path proxying)
"""

import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Heimdall Backend Server")


@app.get("/")
async def home(request: Request):
    """Returns basic server identity information."""
    server_port = request.url.port
    return {
        "message": "Success",
        "handled_by": f"Server running on port {server_port}",
        "timestamp": time.time(),
    }


@app.get("/health")
async def health():
    """
    Health check endpoint polled by Heimdall's HealthChecker.
    Returns 200 OK when the server is operational.
    """
    return {"status": "healthy"}


@app.post("/echo")
async def echo(request: Request):
    """
    Echoes the request body back to the caller.
    Useful for verifying that POST body proxying works correctly.
    """
    server_port = request.url.port
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
    else:
        raw = await request.body()
        body = raw.decode("utf-8", errors="replace")

    return {
        "handled_by": f"Server running on port {server_port}",
        "echoed_body": body,
        "content_type": content_type,
    }


@app.get("/slow")
async def slow(request: Request, delay: float = 2.0):
    """
    Responds after an artificial delay.
    Use ?delay=<seconds> to control the wait time (default: 2s).
    Useful for testing timeout handling and least-connections balancing.
    """
    server_port = request.url.port
    await asyncio.sleep(delay)
    return {
        "handled_by": f"Server running on port {server_port}",
        "delayed_seconds": delay,
    }


@app.get("/items/{item_id}")
async def get_item(request: Request, item_id: int):
    """
    Returns a mock item by ID.
    Demonstrates that path parameters are correctly proxied.
    """
    server_port = request.url.port
    # Simulate a small catalog
    catalog = {
        1: {"name": "Mjölnir", "type": "weapon"},
        2: {"name": "Gungnir", "type": "weapon"},
        3: {"name": "Draupnir", "type": "artifact"},
    }

    item = catalog.get(item_id)
    if item is None:
        return JSONResponse(
            status_code=404,
            content={
                "handled_by": f"Server running on port {server_port}",
                "error": f"Item {item_id} not found",
            },
        )

    return {
        "handled_by": f"Server running on port {server_port}",
        "item_id": item_id,
        **item,
    }