"""Dashboard latency over HTTP: live SQL vs rollup vs Redis cache.

    docker compose --profile bench run --rm bench [--orders 50000]

Self-contained: it seeds a dataset through the admin API (so the data goes through the real
Celery worker), waits for the rollups, then measures. Everything is measured from the client
side, including network and JSON, which is what a caller actually experiences.

Run it on a clean database (`docker compose down -v` first) so the numbers describe one
dataset of the size you asked for rather than whatever earlier runs left behind.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx

SCENARIOS = {
    "live SQL (raw orders)": {"source": "live", "cache": "false"},
    "rollup (no cache)": {"cache": "false"},
    "Redis cache hit": {},
}


def pct(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * p))]


def summarize(ms: list[float]) -> dict[str, float]:
    s = sorted(ms)
    return {"p50": statistics.median(s), "p95": pct(s, 0.95), "p99": pct(s, 0.99), "max": s[-1]}


def wait_job(c: httpx.Client, job_id: str, timeout: float = 900) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = c.get(f"/api/v1/admin/jobs/{job_id}").json()
        if body["state"] == "SUCCESS":
            return body["result"]
        if body["state"] in ("FAILURE", "REVOKED"):
            sys.exit(f"job failed: {body}")
        time.sleep(1)
    sys.exit("timed out waiting for the job")


def wait_rollups(c: httpx.Client, seller_ids: list[int], timeout: float = 900) -> None:
    deadline = time.monotonic() + timeout
    pending = set(seller_ids)
    while pending and time.monotonic() < deadline:
        pending = {
            s for s in pending if not c.get(f"/api/v1/sellers/{s}/status").json()["rollup_ready"]
        }
        time.sleep(1)
    if pending:
        sys.exit(f"rollups not ready for sellers {sorted(pending)}")


def sequential(c: httpx.Client, url: str, params: dict, n: int) -> list[float]:
    for _ in range(3):  # warm-up: connections, plan cache, Redis
        c.get(url, params=params).raise_for_status()
    out = []
    for _ in range(n):
        t = time.perf_counter()
        r = c.get(url, params=params)
        out.append((time.perf_counter() - t) * 1000)
        r.raise_for_status()
    return out


def concurrent(base: str, headers: dict, url: str, params: dict, total: int, workers: int) -> dict:
    lat: list[float] = []

    def worker(n: int) -> list[float]:
        out = []
        with httpx.Client(base_url=base, headers=headers, timeout=60) as c:
            for _ in range(n):
                t = time.perf_counter()
                c.get(url, params=params).raise_for_status()
                out.append((time.perf_counter() - t) * 1000)
        return out

    per = total // workers
    t0 = time.perf_counter()
    with ThreadPoolExecutor(workers) as ex:
        for part in ex.map(worker, [per] * workers):
            lat += part
    wall = time.perf_counter() - t0
    return {"rps": len(lat) / wall, **summarize(lat), "n": len(lat)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8010")
    ap.add_argument("--orders", type=int, default=50_000)
    ap.add_argument("--sellers", type=int, default=5)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--requests", type=int, default=60, help="sequential requests per scenario")
    ap.add_argument("--workers", type=int, default=32, help="client threads for the load test")
    a = ap.parse_args()

    key = os.environ.get("ADMIN_API_KEY")
    if not key:
        sys.exit("ADMIN_API_KEY is not set")
    headers = {"X-API-Key": key}

    with httpx.Client(base_url=a.url, headers=headers, timeout=60) as c:
        c.get("/health").raise_for_status()
        print(f"seeding {a.orders:,} orders for {a.sellers} sellers through the worker queue ...")
        t0 = time.perf_counter()
        job = c.post(
            "/api/v1/admin/seed", json={"sellers": a.sellers, "orders": a.orders, "days": a.days}
        ).json()
        res = wait_job(c, job["job_id"])
        wait_rollups(c, res["seller_ids"])
        print(
            f"  generated in {res['seconds']}s, all rollups ready after {time.perf_counter() - t0:.1f}s"
        )

        seller = res["seller_ids"][0]  # the largest seller (Zipf weighting)
        url = f"/api/v1/sellers/{seller}/dashboard"
        today = datetime.now(UTC).date()
        window = {
            "date_from": (today - timedelta(days=89)).isoformat(),
            "date_to": today.isoformat(),
        }
        base_params = {**window, "group_by": "day", "top_skus": 5}

        # Make sure the three paths agree before timing anything.
        live = c.get(url, params={**base_params, "source": "live", "cache": "false"}).json()
        roll = c.get(url, params={**base_params, "cache": "false"}).json()
        for body in (live, roll):
            body.pop("meta")
        if live != roll:
            sys.exit("live and rollup answers differ: refusing to benchmark wrong numbers")
        orders_in_window = live["totals"]["orders"]
        print(
            f"  seller {seller}: {orders_in_window:,} orders in the 90-day window; live == rollup verified"
        )

        print(f"\nsequential, {a.requests} requests each (client-side ms):")
        seq = {}
        for name, extra in SCENARIOS.items():
            n = 20 if "live" in name else a.requests
            seq[name] = summarize(sequential(c, url, {**base_params, **extra}, n))
            s = seq[name]
            print(
                f"  {name:<24} p50 {s['p50']:7.1f}  p95 {s['p95']:7.1f}  p99 {s['p99']:7.1f}  max {s['max']:7.1f}"
            )

        print(f"\nunder load, {a.workers} concurrent clients:")
        load = {}
        for name, extra in SCENARIOS.items():
            total = 200 if "live" in name else 3000
            total -= total % a.workers
            load[name] = concurrent(a.url, headers, url, {**base_params, **extra}, total, a.workers)
            s = load[name]
            print(
                f"  {name:<24} {s['rps']:8.0f} req/s   p50 {s['p50']:7.1f}  p95 {s['p95']:7.1f}  p99 {s['p99']:7.1f}  ({s['n']} requests)"
            )

        print("\nfull recalculation of this seller, in the worker:")
        job = c.post(f"/api/v1/sellers/{seller}/recalculate", params={"full": "true"}).json()
        r = wait_job(c, job["job_id"])
        print(f"  {r['days']} days, {r['sku_rows']:,} SKU-day rows in {r['seconds']}s")

    print("\nmarkdown:")
    print("| path | p50 | p95 | p99 | under load (req/s) | under load p95 |")
    print("|---|---:|---:|---:|---:|---:|")
    for name in SCENARIOS:
        s, ld = seq[name], load[name]
        print(
            f"| {name} | {s['p50']:.1f} ms | {s['p95']:.1f} ms | {s['p99']:.1f} ms | {ld['rps']:.0f} | {ld['p95']:.1f} ms |"
        )
    print(
        f"\ndataset: {a.orders:,} orders / {a.sellers} sellers / {a.days} days; "
        f"measured seller has {orders_in_window:,} orders in the window"
    )


if __name__ == "__main__":
    main()
