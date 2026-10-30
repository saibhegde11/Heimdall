# Running and reading the Heimdall benchmarks

The reference run used real HTTP/1.1 connections between a load generator, Uvicorn, Heimdall, and controlled backend applications on one Linux machine. It completed 72 measured trials and 54,955 successful requests with zero request errors.

## Run it

From the project folder:

```powershell
python.exe -m pytest -q
python.exe -m benchmarks.run_benchmark
```

The default run uses both workloads, four modes, 1/8/32 busy clients, three repetitions, a 0.5-second warmup, and a three-second measured launch window per trial. It takes several minutes and creates a new timestamped folder under `benchmark_results`.

For a shorter smoke run:

```powershell
python.exe -m benchmarks.run_benchmark --duration 1 --warmup 0.25 --repetitions 1 --profiles equal --concurrency 1 8
```

The runner starts and stops only the server processes it creates, using automatically selected loopback ports. It creates temporary configuration files rather than editing `config.json`. To choose an output location, pass `--output` followed by a new folder path. An existing output folder is not overwritten.

## Workloads and controls

Each backend is one Uvicorn process with four concurrent work slots. `GET /work` waits for a slot and then performs an asynchronous sleep. Extra work requests queue behind those slots. The `/health` handler bypasses the work-slot limit.

| Workload | A delay | B delay | Purpose |
| --- | ---: | ---: | --- |
| `equal` | 10 ms | 10 ms | Compare behavior when nominal service times match. |
| `unequal` | 10 ms | 30 ms | Compare behavior when B completes work more slowly. |

The four modes are direct access to A, round robin, smooth weighted round robin, and least-in-flight. Weighted routing uses A:B weights of 2:1 in both workloads. Those weights are a fixed policy, rather than a measurement of the fixture's changing load.

Direct A has one backend's work slots; proxy modes can use both backends. Higher aggregate throughput through the proxy therefore includes the effect of using a second backend. The single-client equal-workload cases are more useful for seeing the extra forwarding overhead.

All processes use one worker, the standard asyncio loop, h11, error-level logging, and disabled access logs. The load generator reuses HTTPX connections with its connection limits set to the selected concurrency. Forwarding and health clients connect directly to their configured backends.

Temporary benchmark configurations raise token capacity and refill rate to 1,000,000. The token-check code still runs, but rejection does not dominate the forwarding measurement. The included demo configuration continues to use a burst of 10 and a refill of 2 tokens per second per client IP.

## What is measured

This is a closed-loop workload: each busy client waits for its current request before sending another, with no think time. The number of clients is fixed; the arrival rate follows the response time. Started requests are allowed to finish after the launch window, and actual elapsed time including this drain is used for throughput.

Successful throughput is successful responses divided by that elapsed time. Success requires HTTP 200 and a valid A or B identity in the response body. HTTP errors, transport failures, and invalid payloads are recorded separately. Fast rejection responses cannot inflate the successful-request rate.

Latency is measured at the load generator around the HTTP request, through receipt of the buffered response. Percentiles use successful responses and the nearest-rank definition. A group's displayed rate and percentiles are medians of the three individual trial metrics, rather than the fastest trial or a percentile pooled across trials.

Mode order rotates between repetitions. Every completed trial and sample is retained. Warmup samples are excluded from the measured totals. Proxy counters are read before and after measurement to check the request count, attempt count, retries, rate limiting, and cleanup.

## Selected results

Reference environment: Python 3.12.14, Linux x86-64, nine visible logical CPUs, a cgroup quota equivalent to eight CPUs, and an 8 GiB memory limit. The processes share this machine; the CPU allocation is not a promise of dedicated hardware. Runtime versions and source hashes are recorded in the result folder.

At eight busy clients:

| Mode | Equal successful req/s | Unequal successful req/s | Unequal p95 ms | Unequal A share |
| --- | ---: | ---: | ---: | ---: |
| Direct A | 352.73 | 359.75 | 23.52 | 100.00% |
| Round robin | 486.21 | 254.47 | 56.28 | 49.98% |
| Weighted 2:1 | 515.74 | 363.88 | 37.26 | 66.67% |
| Least-in-flight | 514.64 | 386.66 | 36.83 | 70.87% |

In that unequal eight-client case, least-in-flight had about 51.9% higher median successful throughput than round robin. It sent a larger share of completed requests to A while B remained slower. This is an observation for that workload and configuration.

At 32 busy clients in the unequal workload:

| Mode | Successful req/s | p95 ms | p99 ms |
| --- | ---: | ---: | ---: |
| Direct A | 305.50 | 352.95 | 614.83 |
| Round robin | 254.47 | 240.17 | 400.45 |
| Weighted 2:1 | 295.71 | 388.57 | 724.52 |
| Least-in-flight | 291.95 | 428.89 | 722.39 |

The heavier load did not improve throughput. Weighted and least-in-flight also had worse tail latency than round robin in that case. Shared resources, connection pooling, queueing, and the generator can influence these observations; this run did not profile them sufficiently to assign a single cause. The algorithms do not have one universal performance ordering.

At one client in the unequal workload, least-in-flight mostly alternated because both counts returned to zero between requests. It measured about 40 successful requests/s, while weighted routing measured about 46.5. Active-count routing has little load information to use when requests do not overlap.

## Inspect the evidence

The reference folder is `benchmark_results/reference_linux`:

* `BENCHMARK_RESULTS.md`: all 24 grouped comparisons, including rate ranges.
* `summary.csv`: grouped medians, ranges, error counts, and backend shares.
* `trials.csv`: all 72 trial summaries.
* `raw_trials.jsonl.gz`: environment metadata and every measured response sample.
* `environment.json`: versions, workload arguments, and source hashes.

The 72-case matrix was complete. All 54,955 requests succeeded. Every proxy trial's request and attempt deltas matched the measured request count, with zero retries, zero rate-limit rejections, and zero remaining active attempts. Eleven source-file hashes matched the code used in the run.

The reference data were measured on Linux. Running the same script on your Windows machine creates a separate result set, and its numbers can differ.

The highest tested concurrency is 32 busy benchmark clients. Production user capacity would require arrival-rate workloads, think time, service objectives, representative payloads and dependencies, and an independently sized generator. These synthetic I/O fixtures and short loopback trials establish behavior under the stated conditions.

References:

* [HTTPX connection reuse](https://www.python-httpx.org/advanced/clients/)
* [Open and closed load models](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/)

The shipped reference raw data are gzip-compressed. New benchmark runs write plain JSON Lines in their own output folders.
