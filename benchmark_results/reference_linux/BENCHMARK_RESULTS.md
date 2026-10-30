# Heimdall HTTP benchmark results

Run started: 2026-10-30T14:33:38.875745+00:00

Environment: Linux-6.18.57-x86_64-with-glibc2.39; Python 3.12.14.

Completed 72 measured trials, 54955 requests, and 0 errors.

Proxy counter mismatches: 0. Trials leaving active attempts: 0.

Each displayed rate and latency is the median of the individual trial metrics. Rate ranges show minimum to maximum across those trials. A share is aggregated across successful responses.

| Profile | Mode | Busy clients | Trials | Successful req/s | Rate range | p50 ms | p95 ms | p99 ms | Errors | A share |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| equal | direct_a | 1 | 3 | 82.20 | 81.97 to 83.89 | 11.90 | 12.96 | 14.87 | 0 | 100.00% |
| equal | round_robin | 1 | 3 | 72.12 | 71.08 to 72.32 | 13.68 | 15.26 | 16.04 | 0 | 50.00% |
| equal | weighted_round_robin | 1 | 3 | 70.32 | 66.97 to 72.14 | 13.92 | 16.05 | 16.87 | 0 | 66.61% |
| equal | least_in_flight | 1 | 3 | 69.25 | 66.32 to 71.82 | 14.19 | 16.09 | 17.99 | 0 | 50.08% |
| equal | direct_a | 8 | 3 | 352.73 | 344.95 to 357.02 | 22.53 | 24.72 | 27.35 | 0 | 100.00% |
| equal | round_robin | 8 | 3 | 486.21 | 482.78 to 508.51 | 14.95 | 20.89 | 28.90 | 0 | 50.01% |
| equal | weighted_round_robin | 8 | 3 | 515.74 | 483.81 to 520.24 | 14.97 | 18.51 | 26.27 | 0 | 66.69% |
| equal | least_in_flight | 8 | 3 | 514.64 | 425.05 to 518.73 | 14.96 | 18.89 | 24.98 | 0 | 50.77% |
| equal | direct_a | 32 | 3 | 298.28 | 267.11 to 305.32 | 39.83 | 370.97 | 605.73 | 0 | 100.00% |
| equal | round_robin | 32 | 3 | 273.97 | 272.09 to 304.62 | 39.02 | 408.32 | 671.66 | 0 | 50.00% |
| equal | weighted_round_robin | 32 | 3 | 295.78 | 274.45 to 325.35 | 39.46 | 393.58 | 600.01 | 0 | 66.65% |
| equal | least_in_flight | 32 | 3 | 288.18 | 263.99 to 295.30 | 35.19 | 389.63 | 668.58 | 0 | 53.88% |
| unequal | direct_a | 1 | 3 | 83.31 | 82.62 to 84.94 | 11.79 | 12.76 | 14.33 | 0 | 100.00% |
| unequal | round_robin | 1 | 3 | 40.33 | 38.72 to 40.33 | 20.71 | 35.78 | 38.66 | 0 | 50.00% |
| unequal | weighted_round_robin | 1 | 3 | 46.49 | 46.25 to 47.63 | 14.87 | 35.97 | 36.63 | 0 | 66.75% |
| unequal | least_in_flight | 1 | 3 | 40.00 | 39.99 to 40.68 | 33.71 | 36.15 | 37.12 | 0 | 49.73% |
| unequal | direct_a | 8 | 3 | 359.75 | 357.55 to 359.94 | 22.17 | 23.52 | 25.22 | 0 | 100.00% |
| unequal | round_robin | 8 | 3 | 254.47 | 252.78 to 255.35 | 33.46 | 56.28 | 59.69 | 0 | 49.98% |
| unequal | weighted_round_robin | 8 | 3 | 363.88 | 359.61 to 366.79 | 15.59 | 37.26 | 40.57 | 0 | 66.67% |
| unequal | least_in_flight | 8 | 3 | 386.66 | 370.36 to 387.55 | 15.04 | 36.83 | 40.53 | 0 | 70.87% |
| unequal | direct_a | 32 | 3 | 305.50 | 293.30 to 314.00 | 35.00 | 352.95 | 614.83 | 0 | 100.00% |
| unequal | round_robin | 32 | 3 | 254.47 | 253.12 to 255.60 | 68.00 | 240.17 | 400.45 | 0 | 50.02% |
| unequal | weighted_round_robin | 32 | 3 | 295.71 | 295.45 to 311.86 | 47.71 | 388.57 | 724.52 | 0 | 66.63% |
| unequal | least_in_flight | 32 | 3 | 291.95 | 284.92 to 297.76 | 46.93 | 428.89 | 722.39 | 0 | 71.04% |

## Method

All load and server processes ran on this machine over loopback TCP. Each server used one Uvicorn worker, asyncio, h11, error-level logging, and disabled access logs. HTTPX reused connections. Warmup requests were excluded. Clients launched another request only after the prior one completed. All launched requests drained before throughput was calculated.

The fixture represents limited concurrent I/O work using four work slots by default and asynchronous sleeps. Equal profile: A 10 ms, B 10 ms. Unequal profile: A 10 ms, B 30 ms. Health probes bypass the work slots. Direct A uses one backend; proxy modes use both. Weighted routing uses 2:1 weights in both profiles.

Successful requests require HTTP 200 and an A or B server identity in the response. Latency percentiles use successful responses and the nearest-rank definition. HTTP, transport, and invalid-payload failures are retained separately. Successful throughput divides successes by actual elapsed time including drain, rather than the nominal launch window.

Temporary proxy configurations raise token capacity and refill rate to 1,000,000 so rate limiting does not dominate forwarding measurements. The application's default remains a burst of 10 and refill of 2 per second per IP. The actual per-trial configuration is retained in raw_trials.jsonl.gz.

These are synthetic, closed-loop results from a shared machine. The generator, proxy, and backends share its resources. Fixed busy-client concurrency is not a maximum count of real users. No open-loop arrival-rate capacity or production maximum was established. Short trials and local networking limit what the percentiles demonstrate.

Repetition order rotates the modes. The report includes all completed cases rather than selecting the fastest run. Raw samples, status counts, backend counts, proxy counter deltas, configuration values, runtime versions, and source hashes are retained for inspection.
