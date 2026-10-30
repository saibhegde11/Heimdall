import asyncio
from contextlib import asynccontextmanager
import math
import os

from fastapi import FastAPI

NAME = os.environ.get("BENCH_BACKEND_NAME", "A")
DELAY_MS = float(os.environ.get("BENCH_DELAY_MS", "10"))
WORK_SLOTS = int(os.environ.get("BENCH_WORK_SLOTS", "4"))

if NAME not in {"A", "B"}:
    raise ValueError("BENCH_BACKEND_NAME must be A or B")
if not math.isfinite(DELAY_MS) or DELAY_MS < 0:
    raise ValueError("BENCH_DELAY_MS must be finite and nonnegative")
if WORK_SLOTS < 1:
    raise ValueError("BENCH_WORK_SLOTS must be positive")


@asynccontextmanager
async def lifespan(app):
    app.state.work_slots = asyncio.Semaphore(WORK_SLOTS)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "server": NAME}


@app.get("/work")
async def work():
    async with app.state.work_slots:
        await asyncio.sleep(DELAY_MS / 1000)
        return {"server": NAME, "delay_ms": DELAY_MS, "work_slots": WORK_SLOTS}
