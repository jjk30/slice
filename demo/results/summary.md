# slice cost demo — fixed-batch results

**Same 50-prompt workload: $0.258801 direct, $0.141615 through slice — 45.28% cheaper.**

- Baseline model (sent in both legs): `claude-sonnet-4-6`
- Cache-hit signal used: slice 'x-slice-cache: hit' response header
- Paired requests (priced in both legs): 50

## Spend

| | direct | slice |
|---|---|---|
| paired total | $0.258801 | $0.141615 |
| all successful | $0.258801 | $0.141615 |
| successes | 50 | 50 |
| failures | 0 | 0 |

Total saved: $0.117186 (45.28% cheaper on the paired workload).

## Where the savings came from

- **Routing** (cheaper model chosen by slice): $0.087675 across 27 routed request(s).
- **Cache** (identical prompt served from slice's cache at $0): $0.029511 across 5 cache hit(s).

_Routing + cache reconcile exactly to the total saved on the paired set._

## Per-model breakdown — direct leg

| model | requests | cache hits | input tok | output tok | cost |
|---|---|---|---|---|---|
| `claude-sonnet-4-6` | 50 | 0 | 1937 | 16866 | $0.258801 |

## Per-model breakdown — slice leg

| model | requests | cache hits | input tok | output tok | cost |
|---|---|---|---|---|---|
| `claude-haiku-4-5-20251001` | 31 | 4 | 1030 | 7439 | $0.031890 |
| `claude-sonnet-4-6` | 19 | 1 | 907 | 7540 | $0.109725 |

