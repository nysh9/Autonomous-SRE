# Eval results

## Eval run 2026-07-22 12:52:49 — backend: mock

- detection rate: **57%** (7 faults)
- false-alarm rate: **0%** (3 benign)
- root-cause accuracy: **57%**
- fix accuracy: **57%**
- mean time-to-diagnose: **0.14s**
- avg cost/scenario: **$0.000000**  (total $0.000000)

| scenario | expected | predicted | cause | fix | cost | time |
|---|---|---|---|---|---|---|
| service_crash | incident | incident | ✓ | ✓ | $0.000000 | 0.21s |
| database_down | incident | incident | ✓ | ✓ | $0.000000 | 0.17s |
| latency_injection | incident | quiet | — | — | $0.000000 | 0.05s |
| cache_down | incident | incident | ✓ | ✓ | $0.000000 | 0.28s |
| error_rate_spike | incident | quiet | — | — | $0.000000 | 0.07s |
| connection_pool_exhaustion | incident | quiet | — | — | $0.000000 | 0.06s |
| memory_leak | incident | incident | ✓ | ✓ | $0.000000 | 0.14s |
| benign_healthy | benign | quiet | — | — | $0.000000 | 0.07s |
| benign_load_spike | benign | quiet | — | — | $0.000000 | 0.07s |
| benign_minor_errors | benign | quiet | — | — | $0.000000 | 0.07s |

## Eval run 2026-07-22 17:49:59 — backend: mock

- detection rate: **100%** (7 faults)
- false-alarm rate: **0%** (3 benign)
- root-cause accuracy: **100%**
- fix accuracy: **86%**
- mean time-to-diagnose: **0.31s**
- avg cost/scenario: **$0.000000**  (total $0.000000)
- triage-split cost savings: n/a on mock ($0 tokens) — needs an API key

| scenario | expected | predicted | cause | fix | cost | time |
|---|---|---|---|---|---|---|
| service_crash | incident | incident | ✓ | ✓ | $0.000000 | 0.17s |
| database_down | incident | incident | ✓ | ✓ | $0.000000 | 0.14s |
| latency_injection | incident | incident | ✓ | ✓ | $0.000000 | 0.97s |
| cache_down | incident | incident | ✓ | ✓ | $0.000000 | 0.2s |
| error_rate_spike | incident | incident | ✓ | ✗ | $0.000000 | 0.15s |
| connection_pool_exhaustion | incident | incident | ✓ | ✓ | $0.000000 | 0.36s |
| memory_leak | incident | incident | ✓ | ✓ | $0.000000 | 0.18s |
| benign_healthy | benign | quiet | — | — | $0.000000 | 0.19s |
| benign_load_spike | benign | quiet | — | — | $0.000000 | 0.07s |
| benign_minor_errors | benign | quiet | — | — | $0.000000 | 0.07s |
