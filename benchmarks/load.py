import asyncio
from collections import Counter, defaultdict
import math
import statistics
import time

import httpx


def percentile(values, proportion):
    """Nearest-rank percentile; an empty sample has no percentile."""
    if not 0 < proportion <= 1:
        raise ValueError("Percentile proportion must be in (0, 1]")
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * proportion) - 1]


def summarize_samples(samples, elapsed_seconds):
    if not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0:
        raise ValueError("Elapsed time must be positive and finite")
    successes = [sample for sample in samples if sample["ok"]]
    latencies = [sample["latency_ms"] for sample in successes]
    return {
        "elapsed_seconds": elapsed_seconds,
        "requests": len(samples),
        "successes": len(successes),
        "errors": len(samples) - len(successes),
        "http_errors": sum(s["status_code"] not in (None, 200) for s in samples),
        "transport_errors": sum(s["status_code"] is None for s in samples),
        "payload_errors": sum(s["status_code"] == 200 and not s["ok"] for s in samples),
        "successful_rps": len(successes) / elapsed_seconds,
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99),
        "status_counts": dict(Counter(str(s["status_code"]) for s in samples)),
        "error_types": dict(Counter(s["error_type"] for s in samples if s["error_type"])),
        "backend_counts": dict(Counter(s["server"] for s in successes)),
    }


async def run_load(client, url, concurrency, duration):
    """Closed-loop clients issue new requests until the launch window ends.

    All started requests drain before elapsed time and throughput are finalized.
    The caller owns the pooled AsyncClient and its connection limits.
    """
    samples = []
    gate = asyncio.Event()
    window = {}

    async def worker():
        await gate.wait()
        while time.perf_counter() < window["deadline"]:
            started = time.perf_counter()
            sample = {
                "started_offset_ms": (started - window["started"]) * 1000,
                "status_code": None,
                "server": None,
                "error_type": None,
                "ok": False,
            }
            try:
                response = await client.get(url, follow_redirects=False)
                sample["latency_ms"] = (time.perf_counter() - started) * 1000
                sample["status_code"] = response.status_code
                if response.status_code == 200:
                    try:
                        payload = response.json()
                        server = payload.get("server") if isinstance(payload, dict) else None
                    except ValueError:
                        server = None
                    if isinstance(server, str) and server in {"A", "B"}:
                        sample["server"] = server
                        sample["ok"] = True
                    else:
                        sample["error_type"] = "InvalidPayload"
            except httpx.RequestError as error:
                sample["latency_ms"] = (time.perf_counter() - started) * 1000
                sample["error_type"] = type(error).__name__
            samples.append(sample)

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    window["started"] = time.perf_counter()
    window["deadline"] = window["started"] + duration
    gate.set()
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - window["started"]
    return {**summarize_samples(samples, elapsed), "samples": samples}


def aggregate_trials(trials):
    groups = defaultdict(list)
    for trial in trials:
        groups[(trial["profile"], trial["mode"], trial["concurrency"])].append(trial)
    rows = []
    for (profile, mode, concurrency), group in sorted(groups.items()):
        backend_counts = Counter()
        for trial in group:
            backend_counts.update(trial["backend_counts"])
        successes = sum(trial["successes"] for trial in group)
        rates = [trial["successful_rps"] for trial in group]

        def median_latency(key):
            values = [trial[key] for trial in group]
            return statistics.median(values) if all(v is not None for v in values) else None

        rows.append({
            "profile": profile,
            "mode": mode,
            "concurrency": concurrency,
            "trials": len(group),
            "successful_rps_median": statistics.median(rates),
            "successful_rps_min": min(rates),
            "successful_rps_max": max(rates),
            "p50_ms_median": median_latency("p50_ms"),
            "p95_ms_median": median_latency("p95_ms"),
            "p99_ms_median": median_latency("p99_ms"),
            "requests": sum(trial["requests"] for trial in group),
            "successes": successes,
            "errors": sum(trial["errors"] for trial in group),
            "backend_a_share_percent": backend_counts["A"] / successes * 100 if successes else None,
        })
    return rows
