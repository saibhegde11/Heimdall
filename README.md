# Heimdall

Heimdall is a small HTTP load balancer and reverse proxy built with FastAPI and HTTPX. I built it to understand what happens between receiving an HTTP request, choosing a backend, waiting for its response, and handling a failure.

The project started with two servers returning A and B. I added forwarding and round robin, then worked through asynchronous I/O, connection reuse, health monitoring, retries, time budgets, other scheduling policies, rate limits, configuration, and operational statistics. The current build includes 330 automated test cases and a reproducible real-HTTP benchmark.

## What it does

* Forwards GET and POST requests, including body bytes and repeated query parameters.
* Returns backend status codes, response bodies, content types, and redirect locations.
* Chooses healthy backends using round robin, smooth weighted round robin, or least-in-flight.
* Checks health at startup and in a background task, including recovery after an outage.
* Tries at most two distinct backends for a GET after HTTPX request failures. POST gets one attempt.
* Applies a shared forwarding deadline across attempts.
* Uses a token bucket per client address and returns 429 with Retry-After when exhausted.
* Loads validated startup settings from JSON.
* Exposes local statistics and writes structured forwarding and health events.

## Run locally

Python 3.12 was used for the reference tests and benchmarks. The code requires Python 3.11 or later for `asyncio.timeout()`.

From the project folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run each command in a separate terminal:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend_a:app --host 127.0.0.1 --port 8001
.\.venv\Scripts\python.exe -m uvicorn backend_b:app --host 127.0.0.1 --port 8002
.\.venv\Scripts\python.exe -m uvicorn proxy:app --host 127.0.0.1 --port 8000
```

On Linux or macOS, use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe`.

Try a few requests:

```powershell
1..6 | ForEach-Object {
    (Invoke-RestMethod "http://127.0.0.1:8000/").server
}

Invoke-RestMethod "http://127.0.0.1:8000/stats" |
    ConvertTo-Json -Depth 5
```

With both demo backends healthy, a fresh proxy, and the default least-in-flight configuration, the sequential calls alternate A and B. Each previous attempt has finished, so tie rotation makes the choice.

The demo backends also provide `/items/42`, `/search`, `/echo`, and `/go`. For example:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/search?q=python&tag=backend&tag=fastapi"

$body = '{"message":"hello","count":2}'
Invoke-WebRequest "http://127.0.0.1:8000/echo" -Method POST -ContentType "application/json" -Body $body

curl.exe -i "http://127.0.0.1:8000/go"
```

## Configuration

`config.json` supplies the backend origins and weights, algorithm, HTTPX timeouts, health-scan interval, forwarding budget, and token-bucket settings. The included values use ports 8001/8002, A:B weights of 2:1, and the `least_in_flight` algorithm.

Supported algorithm values:

```text
round_robin
weighted_round_robin
least_in_flight
```

Weighted round robin uses configured weights. Least-in-flight scans eligible backends, chooses the smallest active-attempt count, rotates among ties, and reserves the selected slot under the same lock.

The default bucket holds 10 tokens and refills at 2 tokens per second. Its key is the ASGI server's client address. Clients sharing that address share the allowance. Changing the source port does not create another bucket.

To select a custom JSON file you have created:

```powershell
$env:HEIMDALL_CONFIG = ".\config.custom.json"
.\.venv\Scripts\python.exe -m uvicorn proxy:app --host 127.0.0.1 --port 8000
```

Otherwise, the default file is located beside `config.py`. Invalid configuration prevents startup. Unknown fields, empty backend lists, duplicate normalized origins, invalid weights, non-finite values, and unsupported algorithm names are rejected. Backend addresses must be HTTP or HTTPS origins, with no credentials, path prefix, query, or fragment.

Settings are loaded and cached at startup. Restart the proxy after editing JSON. Upstream clients use direct connections with `trust_env=False`.

## Request and failure behavior

The proxy reads the latest completed health snapshot rather than probing every backend for every request. A backend can fail after a successful probe. GET failover helps with that gap, while the background monitor updates future eligibility.

| Situation | Behavior |
| --- | --- |
| Backend sends an HTTP response, including 404 or 500 | Return that response without retrying its status. |
| GET encounters an HTTPX request failure | Try one other eligible, untried backend if available and within the budget. |
| POST encounters a request failure | One attempt; return a controlled error. |
| No eligible backend before any attempt | 503. |
| Attempts end with a non-timeout request failure | 502. |
| Attempts end with an HTTPX timeout, or the shared deadline expires | 504. |
| Token bucket has less than one token | 429 with Retry-After. |

The shared deadline defaults to three seconds and starts after the incoming body has been read. HTTPX's two-second forwarding timeout applies to network phases. Timing out an await does not undo work a backend may already have performed.

## Statistics and logs

`GET /stats` is handled by the proxy before the catch-all route. It reports the four counters, selected algorithm, configured backends, latest healthy list, and in-flight counts. It does not spend tokens, increment the forwarding counters, or contact a backend.

| Counter | Meaning |
| --- | --- |
| `requests_total` | Incoming requests entering the forwarding helper, including rate-limited requests. |
| `backend_attempts_total` | Backend forwarding calls started. |
| `retries_total` | Actual second attempts. |
| `rate_limited_total` | Requests rejected by the token check. |

One GET that tries A and then B counts as one request, two attempts, and one retry. In-flight gauges return to zero after success, failure, or cancellation through `finally` cleanup.

Log messages contain JSON events. A generated request ID links attempts belonging to one incoming forwarding call. Response events include status and duration; request failures include the exception type. The new event fields omit request bodies, request headers, query strings, and raw exception messages. Uvicorn's access logger has its own output format.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The delivered snapshot passed **330 cases** on Linux with Python 3.12.14, including a run treating warnings as errors. The suite covers scheduling, forwarding, health and lifecycle transitions, GET retry limits, POST ambiguity, deadlines, reservation cleanup, rate limits, configuration, statistics, logs, and benchmark calculations.

Tests use mock and ASGI transports, event-controlled asynchronous scenarios, temporary configuration files, and fresh Python processes. Running pytest does not require listening backend servers. The benchmark runner performs the separate real-network measurements.

## Benchmarks

The reference run completed **72 trials and 54,955 measured HTTP requests**, with **zero request errors**. It used 1, 8, and 32 busy clients, three repetitions per case, and equal and unequal synthetic I/O workloads. Each backend had four work slots. All processes and the generator ran on one Linux machine over loopback TCP.

At eight busy clients:

| Mode | Equal backend speeds, successful req/s | B slower, successful req/s | B slower, p95 ms |
| --- | ---: | ---: | ---: |
| Direct A | 352.73 | 359.75 | 23.52 |
| Round robin | 486.21 | 254.47 | 56.28 |
| Weighted 2:1 | 515.74 | 363.88 | 37.26 |
| Least-in-flight | 514.64 | 386.66 | 36.83 |

For the unequal eight-client workload, least-in-flight measured about 51.9% higher median successful throughput than round robin. It sent about 70.9% of successful requests to the faster A backend. Weighted routing retained its configured 2:1 share.

The 32-client unequal case also shows a tradeoff:

| Proxy algorithm | Successful req/s | p95 ms |
| --- | ---: | ---: |
| Round robin | 254.47 | 240.17 |
| Weighted 2:1 | 295.71 | 388.57 |
| Least-in-flight | 291.95 | 428.89 |

Throughput did not keep increasing with concurrency, and the higher-throughput policies had worse p95 latency in that case. The results include queueing and pooling effects in a shared-machine, closed-loop setup. They do not establish production user capacity or a universal winning algorithm.

Direct A uses one backend; proxy modes use two. The reported values are medians of per-trial metrics. Temporary benchmark configs raise the token allowance so successful forwarding is measured instead of mostly producing 429 responses. The normal demo rate limit remains 10 burst tokens and 2 tokens per second.

* [Method and reproduction instructions](BENCHMARKS.md)
* [All grouped results and ranges](benchmark_results/reference_linux/BENCHMARK_RESULTS.md)
* [Summary CSV](benchmark_results/reference_linux/summary.csv)
* [All trial summaries](benchmark_results/reference_linux/trials.csv)
* [Compressed raw measurements](benchmark_results/reference_linux/raw_trials.jsonl.gz)
* [Environment, versions, and source hashes](benchmark_results/reference_linux/environment.json)

Run a new benchmark:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.run_benchmark
```

The runner manages its own child servers on separate available ports and writes a new result folder. It preserves every trial, validates response identities, checks proxy counters, and records the measured source hashes.

## Scope

This is a learning implementation of an HTTP application-level proxy. It currently supports GET and POST, buffers request and response bodies, forwards the incoming Content-Type, and preserves the backend Content-Type and Location. Other headers, methods, streaming, WebSockets, session affinity, and absolute redirect rewriting need additional work.

Scheduler state, token buckets, health snapshots, and counters are local to one process. Idle client buckets are not evicted. The stats endpoint has no authentication in this demo. Backend health is a sampled status check, and an unexpected monitor exception is not supervised automatically.

The shared outbound client also has a cookie jar; supporting independent users' backend sessions would require an explicit stateless forwarding policy. Multiple workers would need coordinated limits and observability if one shared global policy were required.

These boundaries are kept explicit so the code, tests, and benchmark claims describe the same implementation.
