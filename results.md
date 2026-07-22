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
