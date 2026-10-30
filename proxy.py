from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.responses import JSONResponse
import balancer
from balancer import (
    BACKENDS,
    choose_untried_backend,
    finish_backend_request,
    get_in_flight,
)
import httpx
import asyncio
import json
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from health_checks import get_healthy_backends
from rate_limiter import allow_request
from config import get_settings
from metrics import get_totals, increment_counter

settings = get_settings()
BACKEND_TIMEOUT = settings.backend_timeout
HEALTH_CHECK_TIMEOUT = settings.health_check_timeout
HEALTH_CHECK_INTERVAL = settings.health_check_interval
FORWARDING_TIMEOUT = settings.forwarding_timeout
logger = logging.getLogger("uvicorn.error.heimdall")


def log_event(level: int, event: str, **fields) -> None:
    if logger.isEnabledFor(level):
        logger.log(level, json.dumps({"event": event, **fields}))


async def monitor_backends(
    app : FastAPI,
    health_client : httpx.AsyncClient,
) : 
    while True : 
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)

        healthy_backends = await get_healthy_backends(
            health_client,
            BACKENDS,
        )

        if healthy_backends != app.state.healthy_backends : 
            app.state.healthy_backends = healthy_backends
            log_event(
                logging.INFO,
                "health_snapshot_changed",
                healthy_backends=healthy_backends,
            )

@asynccontextmanager
async def lifespan(app : FastAPI) : 
    async with (
        httpx.AsyncClient(timeout=BACKEND_TIMEOUT, trust_env=False) as client,
        httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT, trust_env=False) as health_client,
    ):
        app.state.http_client = client
        app.state.healthy_backends = await get_healthy_backends(
            health_client, 
            BACKENDS,
        )
        log_event(
            logging.INFO,
            "health_snapshot_initialized",
            healthy_backends=app.state.healthy_backends,
        )
        health_task = asyncio.create_task(
            monitor_backends(app, health_client)
        )
        try : 
            yield
        finally : 
            health_task.cancel()

            try : 
                await health_task
            except asyncio.CancelledError : 
                pass

app = FastAPI(lifespan = lifespan)

async def fetch_from_backend(path: str, request: Request):
    increment_counter("requests_total")
    request_id = uuid.uuid4().hex
    client_ip = request.client.host if request.client is not None else "unknown"
    allowed, retry_after = allow_request(client_ip)
    if not allowed:
        increment_counter("rate_limited_total")
        retry_seconds = max(1, math.ceil(retry_after))
        log_event(
            logging.INFO,
            "rate_limited",
            request_id=request_id,
            method=request.method,
            status_code=429,
            retry_after_seconds=retry_seconds,
        )
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests",
            headers={
                "Retry-After": str(retry_seconds),
                "Cache-Control": "no-store",
            },
        )

    body = await request.body()
    client = app.state.http_client

    headers = {}
    content_type = request.headers.get("content-type")
    if content_type is not None:
        headers["content-type"] = content_type

    attempted = set()
    last_error = None
    max_attempts = 2 if request.method == "GET" else 1

    try:
        async with asyncio.timeout(FORWARDING_TIMEOUT):
            for attempt_number in range(1, max_attempts + 1):
                url = choose_untried_backend(
                    app.state.healthy_backends,
                    attempted,
                )

                if url is None:
                    break

                attempted.add(url)
                started = time.perf_counter()
                attempt_fields = {
                    "request_id": request_id,
                    "method": request.method,
                    "backend": url,
                    "attempt": attempt_number,
                }

                try:
                    increment_counter("backend_attempts_total")
                    if attempt_number > 1:
                        increment_counter("retries_total")
                    response = await client.request(
                        method=request.method,
                        url=f"{url}{path}",
                        params=request.query_params.multi_items(),
                        content=body,
                        headers=headers,
                        follow_redirects=False,
                    )
                except httpx.RequestError as error:
                    last_error = error
                    log_event(
                        logging.WARNING,
                        "backend_request_failed",
                        **attempt_fields,
                        error_type=type(error).__name__,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    )
                    continue
                except asyncio.CancelledError:
                    log_event(
                        logging.INFO,
                        "backend_attempt_cancelled",
                        **attempt_fields,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    )
                    raise
                except Exception as error:
                    log_event(
                        logging.ERROR,
                        "backend_unexpected_error",
                        **attempt_fields,
                        error_type=type(error).__name__,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    )
                    raise
                finally:
                    finish_backend_request(url)

                log_event(
                    logging.INFO,
                    "backend_response",
                    **attempt_fields,
                    status_code=response.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                )
                response_headers = {}
                location = response.headers.get("location")
                if location is not None:
                    response_headers["location"] = location

                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=response_headers,
                    media_type=response.headers.get("content-type"),
                )
    except TimeoutError:
        log_event(
            logging.WARNING,
            "forwarding_deadline_exceeded",
            request_id=request_id,
            method=request.method,
            status_code=504,
            timeout_seconds=FORWARDING_TIMEOUT,
        )
        raise HTTPException(
            status_code=504,
            detail="Backend Request Timed Out",
        )

    if last_error is None:
        log_event(
            logging.WARNING,
            "forwarding_unavailable",
            request_id=request_id,
            method=request.method,
            status_code=503,
        )
        raise HTTPException(
            status_code=503,
            detail="No healthy backends available",
        )

    status_code = 504 if isinstance(last_error, httpx.TimeoutException) else 502
    detail = "Backend Request Timed Out" if status_code == 504 else "Backend Request Failed"
    log_event(
        logging.WARNING,
        "forwarding_failed",
        request_id=request_id,
        method=request.method,
        status_code=status_code,
        error_type=type(last_error).__name__,
    )
    raise HTTPException(
        status_code=status_code,
        detail=detail,
    )


@app.get("/stats")
async def get_stats():
    in_flight = get_in_flight()
    return JSONResponse(
        content={
            **get_totals(),
            "balancing_algorithm": balancer.BALANCING_ALGORITHM,
            "configured_backends": list(BACKENDS),
            "healthy_backends": list(app.state.healthy_backends),
            "in_flight": {
                backend: in_flight.get(backend, 0)
                for backend in BACKENDS
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy_request(path : str, request : Request) :
    return await fetch_from_backend(f"/{path}", request)
