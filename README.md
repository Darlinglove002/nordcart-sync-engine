# nordcart-sync-engine

[![CI](https://github.com/Darlinglove002/nordcart-sync-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Darlinglove002/nordcart-sync-engine/actions/workflows/ci.yml)

```mermaid
flowchart TB
    Client["Seller or admin<br/>X-API-Key"] -- "GET /dashboard<br/>POST /orders (admin)" --> API["FastAPI, 2 workers<br/>auth, validation"]

    API -- "cache lookup" --> Redis[("Redis<br/>version-keyed cache<br/>single-flight lock")]
    API -- "on a miss: read rollups<br/>on ingest: mark seller dirty" --> PG[("PostgreSQL 16<br/>raw: orders, returns, ad_spend<br/>rollups: daily_seller_metrics, daily_sku_metrics")]
    API -. "enqueue recalculation" .-> MQ[["RabbitMQ"]]

    Beat["Celery beat<br/>every 60 s"] -- "recalculate_dirty" --> MQ
    MQ --> Worker["Celery worker<br/>recalculate_seller(id)"]
    Worker -- "rebuild days since dirty_since" --> PG
    Worker -- "invalidate cache after commit" --> Redis
```

B2B backend for marketplace sellers. A built-in generator produces 50 000+ synthetic orders,
returns, commissions and ad spend. A seller asks the REST API for a **unit-economics
dashboard** for any period; Celery workers keep the margins recalculated in the background,
and the answer comes back from Redis in **milliseconds**.

> All data is synthetic. This is a portfolio project about the architecture (API vs. heavy
> background work, cache protection for the database, automated tests), not a connector to a
> real marketplace.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Alembic · Celery ·
RabbitMQ · Redis · pytest · Docker Compose · GitHub Actions

## Quick start

```bash
cp .env.example .env     # set ADMIN_API_KEY (generation command is in the file)
docker compose up -d --build
# 50 000 synthetic orders for 5 sellers; builds the rollups and prints per-seller API keys once
docker compose exec api python -m app.generator --orders 50000 --sellers 5 --reset
```

Interactive docs: <http://127.0.0.1:8010/docs>. Seeding through the queue instead of the CLI
(the generation then runs in a Celery worker): `POST /api/v1/admin/seed` with the admin key.

## Example Request & Response

The seeding command from the quick start builds the rollups in-process, logs progress to stderr, and prints the one-time seller API keys (secrets) to stdout only:

```text
2026-11-20 22:34:38,420 INFO    app.generator: existing data wiped
2026-11-20 22:34:40,701 INFO    app.generator: generated 50,000 orders, 3,490 returns, 1,000 products, 900 ad-spend days in 2.3s
2026-11-20 22:34:41,802 INFO    app.generator: rollup seller 1: 180 days, 10,347 sku-day rows in 1.10s
…
seller API keys (shown once):
  seller   1  Fjord & Co #436a-1               <seller key>
  seller   2  Nordic Nest Goods #436a-2        <seller key>
…
```

Ask for seller 1's unit economics, aggregated by month:

```bash
curl -H "X-API-Key: <seller key>" \
  "http://127.0.0.1:8010/api/v1/sellers/1/dashboard?date_from=2026-08-01&date_to=2026-11-18&group_by=month&top_skus=1"
```

Real response, trimmed to the first month and one best/worst SKU (the full one has four monthly rows). Amounts are integer cents; `net_profit_cents` is exactly the formula under [Unit economics](#unit-economics):

```json
{
  "seller_id": 1, "date_from": "2026-08-01", "date_to": "2026-11-18", "group_by": "month",
  "totals": {
    "orders": 10720, "units": 13797, "revenue_cents": 205973686, "cogs_cents": 142128615,
    "commission_cents": 20448281, "logistics_cents": 6527414, "ad_spend_cents": 13186248,
    "returns_count": 896, "refunds_cents": 12419192, "commission_refund_cents": 1334303,
    "return_logistics_cents": 441643, "restocked_cogs_cents": 5784698,
    "net_profit_cents": 17941294, "margin_pct": 8.71, "return_rate_pct": 8.36,
    "avg_profit_per_order_cents": 1673, "loss_making_skus": 21, "active_skus": 200
  },
  "series": [
    {"period": "2026-08-01", "orders": 2661, "revenue_cents": 52122016, "net_profit_cents": 4549019, "margin_pct": 8.73, "returns_count": 194}
  ],
  "top_skus": [
    {"sku": "SKU-1-0003", "category": "electronics", "units": 555, "revenue_cents": 42428446, "net_profit_cents": 2601442, "margin_pct": 6.13, "return_rate_pct": 6.03}
  ],
  "worst_skus": [
    {"sku": "SKU-1-0080", "category": "home", "units": 42, "revenue_cents": 1469131, "net_profit_cents": -197128, "margin_pct": -13.42, "return_rate_pct": 3.33}
  ],
  "meta": {"source": "rollup", "computed_at": "2026-11-20T16:34:46+00:00", "rollup_updated_at": "2026-11-20T16:34:40+00:00", "stale": false}
}
```

The generator is deterministic: two separate seedings of this data set gave byte-identical `totals`. The first call above was `X-Cache: MISS` (computed from the rollup tables), the repeat `X-Cache: HIT` with an identical body; `X-Cache` is `HIT`, `MISS`, `COALESCED` or `BYPASS`, and `X-Process-Time-Ms` is on every response.

`GET /health` probes PostgreSQL (`SELECT 1`) and Redis (`PING`) with hard timeouts. Healthy: `200`

```json
{"status": "ok", "postgres": "ok", "redis": "ok"}
```

With a component down it answers `503` and names it. Real outputs, with the Redis container frozen (`docker pause`, answered in 0.5 s) and with both containers stopped (5 s):

```json
{"status": "degraded", "postgres": "ok", "redis": "down", "failed": ["redis"]}
{"status": "degraded", "postgres": "down", "redis": "down", "failed": ["postgres", "redis"]}
```

Both recover to `200` as soon as the containers are back. Dashboard requests themselves keep working while Redis is down (see *Fail open* below); `/health` reports the outage so it is visible to an operator or orchestrator.

## Measured performance

Same request (90-day dashboard, one seller), over HTTP, synthetic data, one machine under
Docker Desktop. Full method, caveats and the 500 000-order run: [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).

| path | p50 @ 50k orders | p50 @ 500k orders | req/s under 32 clients @ 500k |
|---|---:|---:|---:|
| live SQL over raw orders | 71.6 ms | 294.5 ms | 8 |
| rollup table (maintained by Celery) | 15.2 ms | 17.9 ms | 149 |
| Redis cache hit | 3.0 ms | 3.9 ms | 485 |

The live query grows with the data; the rollup read and the cache hit do not. That is the
whole design. Reproduce: `docker compose down -v && docker compose --profile bench run --rm bench`.

## Unit economics

All money is **integer cents**; there is no float arithmetic anywhere in the pipeline.

```
revenue     = quantity × unit_price − discount                      (delivered orders only)
commission  = round_half_up(revenue × commission_bps / 10 000)
net profit  = revenue − cogs − commission − logistics − ad_spend
              − refunds + commission_refund − return_logistics + restocked_cogs
```

A return is booked on the day it arrives: the customer is refunded, the marketplace gives the
commission back, the seller pays return logistics, and the goods' cost comes back only if they
are restockable. Cancelled orders do not count. Ad spend is a seller-level cost, so SKU-level
profit excludes it. The rules live in [`app/domain/economics.py`](app/domain/economics.py).

## API

| endpoint | who | what |
|---|---|---|
| `GET /api/v1/sellers/{id}/dashboard` | seller or admin | totals, series (day/week/month), best and worst SKUs |
| `GET /api/v1/sellers/{id}/status` | seller or admin | rollup freshness, pending changes |
| `POST /api/v1/sellers/{id}/recalculate` | seller or admin | queue a recalculation (202, throttled to 1 per 30 s) |
| `POST /api/v1/sellers/{id}/orders` | admin | ingest up to 1 000 orders; the dashboard is flagged `stale` until the worker catches up |
| `POST /api/v1/admin/seed` | admin | generate a dataset in a worker |
| `GET /api/v1/admin/jobs/{id}` | admin | Celery job state |
| `POST /api/v1/admin/sellers/{id}/rotate-key` | admin | new API key (shown once); the old one dies immediately |
| `GET /health` | – | readiness: PostgreSQL `SELECT 1` and Redis `PING`; `200`, or `503` with the failing components in `failed` |

## Design decisions

**One definition of the numbers.** The live path and the worker run the *same two SQL
statements* ([`app/services/sql.py`](app/services/sql.py)), so a rollup cannot drift from the raw
data. Those statements are tested against an independent pure-Python implementation of the
rules on random datasets, and `source=live` (admin only) lets you diff any answer against the
raw tables.

**The API never does the heavy work.** Aggregating raw orders happens in Celery. The API reads
a small per-day rollup (180 rows per seller for half a year) and a per-SKU rollup summed in SQL.
Only a seller that has never been calculated gets a live answer, once, while a rollup is queued.

**Incremental, idempotent recalculation.** New orders set `dirty_since` to the earliest day they
touch and bump `data_version`. A recalculation rebuilds only days ≥ `dirty_since` (delete and
re-insert that range), so running it twice gives the same result and a worker that dies
mid-task can safely be retried (`acks_late`). It clears the dirty flag with a compare-and-set
on `data_version`: rows ingested *while* it ran keep the seller dirty for the next pass instead
of being silently lost. One recalculation per seller at a time (Redis lock); a beat sweep
every 60 s picks up anything missed.

**Cache that protects the database.**
- *Version-keyed invalidation:* keys embed a per-seller version; invalidation is one atomic
  `INCR`, no `SCAN`/`DEL`. It runs after the DB commit, so a reader can never cache
  pre-recalculation data under the new version.
- *Single flight:* on a miss one request takes a short lock and computes; concurrent requests
  for the same key wait and reuse the result (12 concurrent misses = 1 query, tested).
- *Fail open:* if Redis is down, requests are served from PostgreSQL, not with errors.
- A cache hit touches no database connection: the API key → seller lookup is cached in Redis too.

**Security.** Per-seller API keys, stored as SHA-256 (256-bit random keys, so a slow hash adds
nothing); admin key compared in constant time and required to be ≥ 24 characters, with no
default, so the service will not start without one. A seller asking for another seller's id gets
the same 404 as for a seller that does not exist. Inputs are bounded (batch size, quantities,
line totals, date range ≤ 366 days). Unhandled errors return a generic 500, job errors expose
only the exception class. Containers run as a non-root user and every published port is bound to
`127.0.0.1`.

## Tests

```bash
docker compose up -d db redis       # integration tests use their own database and Redis db 15
pip install -e ".[dev]"
pytest -q                            # 104 tests, ~35 s
```

- **Rules:** hand-computed cases, half-up rounding, returns, cancellations, week/month bucketing.
- **SQL vs. reference:** the SQL aggregation equals the pure-Python rules for every day and
  field on random datasets, including a basket whose `revenue × bps` overflows int32.
- **Rollup:** rollup answer == live answer, idempotence, incremental range, the mid-run ingest race.
- **Cache:** hit/miss, per-seller scoping, TTL, atomic invalidation, single flight, a dead lock
  holder, Redis down.
- **API:** auth, cross-tenant 404, admin-only switches, validation (422), all-or-nothing ingest,
  throttling, key rotation vs. the auth cache, no leakage in 500s.
- **Worker:** task registration, lock skip, sweep, cache bump after commit.
- **Ops:** `/health` 200 and 503 for Redis down, PostgreSQL down and both (real refused connections), log level handling, API keys printed to stdout and never logged, `.env.example` in sync with the settings.

CI runs ruff, mypy and the suite on Python 3.11 and 3.12 against real PostgreSQL and Redis service
containers; a missing service fails the build rather than skipping tests. Mutation check done by
hand: removing the `bigint` cast in the SQL makes the suite fail.

One bug the tests found during development: `refund_cents` is int4, so a 100 × 500 000-cent
basket overflowed it. Order line totals are now capped at 2·10⁹ cents in both the API schema and
a `CHECK` constraint.

## Not done / known limits

- No real marketplace connectors; ingestion takes JSON, and the generator is the data source.
- Days are UTC calendar days; there is no per-seller time zone.
- TLS, a reverse proxy and request rate limiting (beyond the recalculation throttle) are left to
  the deployment. The local compose Redis has no password (localhost-bound).
- Returns and ad spend are generated, not ingestible through the API.
- Single PostgreSQL instance; no partitioning of `orders`. Fine for the sizes measured here,
  and the rollup design is what you would keep when adding it.

## Layout

```
app/
  api/        FastAPI app, routes, auth dependencies, schemas
  domain/     economics rules (reference implementation) and dashboard shaping, no I/O
  services/   SQL aggregation, rollup/recalculation, ingestion
  worker/     Celery app (+ beat schedule) and tasks
  generator/  deterministic synthetic data (COPY-based), CLI
  cache.py    Redis cache: versioning, single flight, fail-open
  health.py   dependency probes behind /health (PostgreSQL, Redis)
  logging_config.py  one log format, LOG_LEVEL
  db/         SQLAlchemy models, sessions
migrations/   Alembic
benchmarks/   HTTP benchmark and recorded results
tests/        unit/ and integration/
```
