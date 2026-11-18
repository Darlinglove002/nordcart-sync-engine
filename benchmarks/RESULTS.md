# Benchmark results

Produced by `docker compose --profile bench run --rm bench` (see `benchmarks/bench.py`), one
run per dataset size, each on a freshly created database (`docker compose down -v` first).
Latency is measured from the client, so it includes the network hop and JSON decoding.

**Environment:** Windows 11 desktop machine, Docker Desktop (WSL2); API with 2 uvicorn workers,
PostgreSQL 16, Redis 7, everything on the same machine, no tuning. The load generator is a
32-thread Python client in another container, so the req/s figures for the cache path are
a lower bound set partly by the client. Numbers will differ on other hardware; the *ratios*
and the *shape* (what grows with data and what does not) are the point.

**Request:** `GET /api/v1/sellers/{id}/dashboard`, last 90 days, grouped by day, top 5 SKUs,
for the largest of 5 synthetic sellers. Before timing, the script checks that the live and
rollup answers are identical, field for field, and aborts otherwise.

## 50 000 orders (seller in the window: 9 111 orders)

| path | p50 | p95 | under load (req/s) | under load p95 |
|---|---:|---:|---:|---:|
| live SQL over raw orders | 71.6 ms | 105.4 ms | 24 | 1562.7 ms |
| rollup table (Celery-maintained) | 15.2 ms | 19.3 ms | 161 | 245.6 ms |
| Redis cache hit | 3.0 ms | 3.6 ms | 685 | 61.7 ms |

## 500 000 orders (seller in the window: 90 440 orders)

| path | p50 | p95 | under load (req/s) | under load p95 |
|---|---:|---:|---:|---:|
| live SQL over raw orders | 294.5 ms | 446.9 ms | 8 | 4993.6 ms |
| rollup table (Celery-maintained) | 17.9 ms | 22.8 ms | 149 | 265.3 ms |
| Redis cache hit | 3.9 ms | 4.8 ms | 485 | 92.6 ms |

Worker side, full rebuild of the same seller: **0.74 s** at 50 000 orders (10 371 SKU-day
rows), **2.57 s** at 500 000 orders (32 036 SKU-day rows). Seeding 500 000 orders through the
worker took 20.4 s.

## How to read this

* The live query grows with the data (72 -> 295 ms for 10x the orders); the rollup read and
  the cache hit barely move (15 -> 18 ms, 3.0 -> 3.9 ms). That is the architectural claim.
* Sequential percentiles for the live path come from 20 requests, the others from 60, so
  p95/p99 of the live path are rough. The load tests are 192 and 2 976 requests.
* Single run per size. No warm-vs-cold PostgreSQL page-cache analysis was done: the data
  fits in memory, so live-path numbers are the optimistic ones for a larger real database.
* All data is synthetic (`app/generator`). These are not production numbers of anything.
