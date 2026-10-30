"""Run controlled HTTP benchmarks against owned local Uvicorn processes."""

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import csv
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time

import httpx

from benchmarks.load import aggregate_trials, run_load

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODES = ["direct_a", "round_robin", "weighted_round_robin", "least_in_flight"]
PROFILES = {"equal": {"A": 10, "B": 10}, "unequal": {"A": 10, "B": 30}}
COUNTERS = ["requests_total", "backend_attempts_total", "retries_total", "rate_limited_total"]


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def nonnegative_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative and finite")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=positive_float, default=3.0)
    parser.add_argument("--warmup", type=nonnegative_float, default=0.5)
    parser.add_argument("--concurrency", type=positive_int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--repetitions", type=positive_int, default=3)
    parser.add_argument("--profiles", choices=list(PROFILES), nargs="+", default=list(PROFILES))
    parser.add_argument("--modes", choices=MODES, nargs="+", default=MODES.copy())
    parser.add_argument("--slots", type=positive_int, default=4)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def choose_ports(count):
    sockets = []
    try:
        for _ in range(count):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            sockets.append(listener)
        return [listener.getsockname()[1] for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


@asynccontextmanager
async def server(module, port, log_path, extra_environment=None):
    environment = os.environ.copy()
    environment.pop("HEIMDALL_CONFIG", None)
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment["PYTHONUNBUFFERED"] = "1"
    environment.update(extra_environment or {})
    command = [
        sys.executable, "-m", "uvicorn", module,
        "--host", "127.0.0.1", "--port", str(port),
        "--workers", "1", "--loop", "asyncio", "--http", "h11",
        "--log-level", "error", "--no-access-log",
    ]
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command, cwd=PROJECT_ROOT, env=environment,
            stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5.0)


async def wait_ready(process, url, predicate, log_path):
    deadline = time.perf_counter() + 15.0
    async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
        while time.perf_counter() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Server exited during startup: {log_path.read_text()}")
            try:
                response = await client.get(url)
                if response.status_code == 200 and predicate(response.json()):
                    if process.poll() is None:
                        return
            except (httpx.RequestError, ValueError):
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError(f"Readiness check timed out for {url}; see {log_path}")


def environment_metadata():
    metadata = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "versions": {name: version(name) for name in ["fastapi", "httpx", "pydantic", "uvicorn", "starlette", "anyio", "h11"]},
        "protocol": "HTTP/1.1 over IPv4 loopback TCP",
        "uvicorn": {"workers_per_process": 1, "loop": "asyncio", "http": "h11", "log_level": "error", "access_log": False},
        "load_model": "closed loop; zero think time; drain started requests",
        "network_environment": "load generator and proxy upstream clients use trust_env=False",
        "source_sha256": {},
    }
    if hasattr(os, "sched_getaffinity"):
        metadata["affinity_cpus"] = len(os.sched_getaffinity(0))
    for name, location in [("cpu_quota", "/sys/fs/cgroup/cpu.max"), ("memory_limit", "/sys/fs/cgroup/memory.max")]:
        path = Path(location)
        if path.exists():
            metadata[name] = path.read_text().strip()
    files = ["proxy.py", "balancer.py", "health_checks.py", "rate_limiter.py", "metrics.py", "config.py", "config.json", "requirements.txt", "benchmarks/backend.py", "benchmarks/load.py", "benchmarks/run_benchmark.py"]
    metadata["source_sha256"] = {
        name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest()
        for name in files
    }
    return metadata


async def measure_case(args, profile, mode, concurrency, repetition, backends, output, temporary):
    identifier = f"{profile}_{mode}_c{concurrency}_r{repetition}"
    configuration = None
    async with AsyncExitStack() as stack:
        if mode == "direct_a":
            target = backends[0]
        else:
            configuration = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
            configuration.update({
                "backends": [{"url": backends[0], "weight": 2}, {"url": backends[1], "weight": 1}],
                "balancing_algorithm": mode,
                "rate_limit_capacity": 1000000,
                "rate_limit_refill_rate": 1000000.0,
            })
            path = temporary / f"{identifier}.json"
            path.write_text(json.dumps(configuration), encoding="utf-8")
            port = choose_ports(1)[0]
            target = f"http://127.0.0.1:{port}"
            log_path = output / "server_logs" / f"{identifier}.log"
            process = await stack.enter_async_context(server(
                "proxy:app", port, log_path, {"HEIMDALL_CONFIG": str(path)},
            ))
            await wait_ready(
                process, target + "/stats",
                lambda body: body["balancing_algorithm"] == mode and set(body["healthy_backends"]) == set(backends),
                log_path,
            )

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            timeout=10.0, trust_env=False, http2=False,
        ) as client:
            warmup = await run_load(client, target + "/work", concurrency, args.warmup) if args.warmup else None
            before = (await client.get(target + "/stats")).json() if mode != "direct_a" else None
            measured = await run_load(client, target + "/work", concurrency, args.duration)
            after = (await client.get(target + "/stats")).json() if mode != "direct_a" else None
        delta = {key: after[key] - before[key] for key in COUNTERS} if after is not None else None
        if delta is not None:
            counter_match = delta["requests_total"] == measured["requests"]
            all_idle = all(value == 0 for value in after["in_flight"].values())
        else:
            counter_match = None
            all_idle = None
        return {
            "record_type": "trial",
            "profile": profile, "mode": mode, "concurrency": concurrency,
            "repetition": repetition, "duration_seconds": args.duration,
            "warmup_seconds": args.warmup, "work_slots_per_backend": args.slots,
            "service_delays_ms": PROFILES[profile],
            "target": target, "proxy_configuration": configuration,
            "warmup": {key: value for key, value in warmup.items() if key != "samples"} if warmup else None,
            "proxy_metrics_delta": delta,
            "proxy_request_counter_matches": counter_match,
            "all_backend_attempts_finished": all_idle,
            **measured,
        }


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(output, metadata, trials):
    summary = aggregate_trials(trials)
    write_csv(output / "summary.csv", summary)
    fields = ["profile", "mode", "concurrency", "repetition", "elapsed_seconds", "requests", "successes", "errors", "http_errors", "transport_errors", "payload_errors", "successful_rps", "p50_ms", "p95_ms", "p99_ms"]
    write_csv(output / "trials.csv", [{key: trial[key] for key in fields} for trial in trials])
    total_requests = sum(trial["requests"] for trial in trials)
    errors = sum(trial["errors"] for trial in trials)
    mismatches = sum(trial["proxy_request_counter_matches"] is False for trial in trials)
    remaining = sum(trial["all_backend_attempts_finished"] is False for trial in trials)
    lines = [
        "# Heimdall HTTP benchmark results", "",
        f"Run started: {metadata['started_utc']}", "",
        f"Environment: {metadata['platform']}; Python {metadata['python']}.", "",
        f"Completed {len(trials)} measured trials, {total_requests} requests, and {errors} errors.", "",
        f"Proxy counter mismatches: {mismatches}. Trials leaving active attempts: {remaining}.", "",
        "Each displayed rate and latency is the median of the individual trial metrics. Rate ranges show minimum to maximum across those trials. A share is aggregated across successful responses.", "",
        "| Profile | Mode | Busy clients | Trials | Successful req/s | Rate range | p50 ms | p95 ms | p99 ms | Errors | A share |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    def number(value):
        return f"{value:.2f}" if value is not None else "n/a"
    for row in sorted(summary, key=lambda r: (r["profile"], r["concurrency"], MODES.index(r["mode"]))):
        lines.append(
            f"| {row['profile']} | {row['mode']} | {row['concurrency']} | {row['trials']} | {number(row['successful_rps_median'])} | {number(row['successful_rps_min'])} to {number(row['successful_rps_max'])} | {number(row['p50_ms_median'])} | {number(row['p95_ms_median'])} | {number(row['p99_ms_median'])} | {row['errors']} | {number(row['backend_a_share_percent'])}% |"
        )
    lines += [
        "", "## Method", "",
        "All load and server processes ran on this machine over loopback TCP. Each server used one Uvicorn worker, asyncio, h11, error-level logging, and disabled access logs. HTTPX reused connections. Warmup requests were excluded. Clients launched another request only after the prior one completed. All launched requests drained before throughput was calculated.", "",
        "The fixture represents limited concurrent I/O work using four work slots by default and asynchronous sleeps. Equal profile: A 10 ms, B 10 ms. Unequal profile: A 10 ms, B 30 ms. Health probes bypass the work slots. Direct A uses one backend; proxy modes use both. Weighted routing uses 2:1 weights in both profiles.", "",
        "Successful requests require HTTP 200 and an A or B server identity in the response. Latency percentiles use successful responses and the nearest-rank definition. HTTP, transport, and invalid-payload failures are retained separately. Successful throughput divides successes by actual elapsed time including drain, rather than the nominal launch window.", "",
        "Temporary proxy configurations raise token capacity and refill rate to 1,000,000 so rate limiting does not dominate forwarding measurements. The application's default remains a burst of 10 and refill of 2 per second per IP. The actual per-trial configuration is retained in raw_trials.jsonl.", "",
        "These are synthetic, closed-loop results from a shared machine. The generator, proxy, and backends share its resources. Fixed busy-client concurrency is not a maximum count of real users. No open-loop arrival-rate capacity or production maximum was established. Short trials and local networking limit what the percentiles demonstrate.", "",
        "Repetition order rotates the modes. The report includes all completed cases rather than selecting the fastest run. Raw samples, status counts, backend counts, proxy counter deltas, configuration values, runtime versions, and source hashes are retained for inspection.", "",
    ]
    (output / "BENCHMARK_RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


async def run(args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or PROJECT_ROOT / "benchmark_results" / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "server_logs").mkdir()
    metadata = environment_metadata()
    metadata["arguments"] = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    (output / "environment.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    trials = []
    total = len(args.profiles) * len(args.modes) * len(args.concurrency) * args.repetitions
    print(f"Running {total} trials; output: {output}", flush=True)
    with (output / "raw_trials.jsonl").open("w", encoding="utf-8") as raw:
        raw.write(json.dumps({"record_type": "environment", **metadata}, allow_nan=False) + "\n")
        raw.flush()
        with tempfile.TemporaryDirectory(prefix="temporary_configs_", dir=output) as temporary:
            try:
                for profile in args.profiles:
                    ports = choose_ports(2)
                    backends = [f"http://127.0.0.1:{port}" for port in ports]
                    async with AsyncExitStack() as stack:
                        for index, name in enumerate(["A", "B"]):
                            log_path = output / "server_logs" / f"backend_{profile}_{name}.log"
                            process = await stack.enter_async_context(server(
                                "benchmarks.backend:app", ports[index], log_path,
                                {"BENCH_BACKEND_NAME": name, "BENCH_DELAY_MS": str(PROFILES[profile][name]), "BENCH_WORK_SLOTS": str(args.slots)},
                            ))
                            await wait_ready(process, backends[index] + "/health", lambda body, name=name: body.get("server") == name, log_path)
                        for repetition in range(1, args.repetitions + 1):
                            shift = (repetition - 1) % len(args.modes)
                            modes = args.modes[shift:] + args.modes[:shift]
                            for concurrency in args.concurrency:
                                for mode in modes:
                                    trial = await measure_case(args, profile, mode, concurrency, repetition, backends, output, Path(temporary))
                                    trials.append(trial)
                                    raw.write(json.dumps(trial, allow_nan=False) + "\n")
                                    raw.flush()
                                    p95 = trial["p95_ms"]
                                    p95_text = f"{p95:.2f}" if p95 is not None else "n/a"
                                    print(f"[{len(trials)}/{total}] {profile} {mode} clients={concurrency} rep={repetition}: {trial['successful_rps']:.1f} successful req/s, p95={p95_text} ms, errors={trial['errors']}", flush=True)
            finally:
                write_report(output, metadata, trials)
    print(f"Saved {len(trials)} trials to {output}", flush=True)
    return output


def main(argv=None):
    args = parse_args(argv)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
