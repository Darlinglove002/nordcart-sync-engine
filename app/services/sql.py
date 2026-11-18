"""The heavy aggregation, written once.

Both the live path (`source=live`, used for verification and benchmarks) and the Celery
recalculation use these two statements, so a rollup can never drift from the raw data by
construction. The rules mirror `app.domain.economics`; tests compare them on random data.

`(x * bps + 5000) / 10000` is integer division on non-negative bigints: round half up,
identical to `apply_bps` in Python. Everything is cast to bigint first because
quantity * price overflows int4 for large baskets.
"""

from sqlalchemy import text

# One row per day that has any order, return or ad spend in the range.
DAILY_SQL = text(
    """
WITH o AS (
    SELECT
        o.day,
        count(*)                                                      AS orders,
        sum(o.quantity)::bigint                                       AS units,
        sum(rev.v)::bigint                                            AS revenue_cents,
        sum(o.quantity::bigint * p.cost_cents)::bigint                AS cogs_cents,
        sum((rev.v * o.commission_bps + 5000) / 10000)::bigint        AS commission_cents,
        sum(o.logistics_cents::bigint)::bigint                        AS logistics_cents
    FROM orders o
    JOIN products p ON p.id = o.product_id
    CROSS JOIN LATERAL (
        SELECT o.quantity::bigint * o.unit_price_cents - o.discount_cents AS v
    ) rev
    WHERE o.seller_id = :seller_id
      AND o.day BETWEEN :date_from AND :date_to
      AND o.status = 'delivered'
    GROUP BY o.day
),
r AS (
    SELECT
        r.day,
        count(*)                                                      AS returns_count,
        sum(r.refund_cents::bigint)::bigint                           AS refunds_cents,
        sum((r.refund_cents::bigint * o.commission_bps + 5000) / 10000)::bigint
                                                                      AS commission_refund_cents,
        sum(r.return_logistics_cents::bigint)::bigint                 AS return_logistics_cents,
        sum(CASE WHEN r.restockable THEN r.quantity::bigint * p.cost_cents ELSE 0 END)::bigint
                                                                      AS restocked_cogs_cents
    FROM returns r
    JOIN orders o   ON o.id = r.order_id
    JOIN products p ON p.id = o.product_id
    WHERE r.seller_id = :seller_id
      AND r.day BETWEEN :date_from AND :date_to
      AND o.status = 'delivered'
    GROUP BY r.day
),
a AS (
    SELECT day, spend_cents::bigint AS ad_spend_cents
    FROM ad_spend
    WHERE seller_id = :seller_id AND day BETWEEN :date_from AND :date_to
),
days AS (
    SELECT day FROM o UNION SELECT day FROM r UNION SELECT day FROM a
)
SELECT
    d.day,
    coalesce(o.orders, 0)                  AS orders,
    coalesce(o.units, 0)                   AS units,
    coalesce(o.revenue_cents, 0)           AS revenue_cents,
    coalesce(o.cogs_cents, 0)              AS cogs_cents,
    coalesce(o.commission_cents, 0)        AS commission_cents,
    coalesce(o.logistics_cents, 0)         AS logistics_cents,
    coalesce(a.ad_spend_cents, 0)          AS ad_spend_cents,
    coalesce(r.returns_count, 0)           AS returns_count,
    coalesce(r.refunds_cents, 0)           AS refunds_cents,
    coalesce(r.commission_refund_cents, 0) AS commission_refund_cents,
    coalesce(r.return_logistics_cents, 0)  AS return_logistics_cents,
    coalesce(r.restocked_cogs_cents, 0)    AS restocked_cogs_cents
FROM days d
LEFT JOIN o USING (day)
LEFT JOIN r USING (day)
LEFT JOIN a USING (day)
ORDER BY d.day
"""
)

# One row per (day, product). Ad spend is not attributable to a SKU, so it is absent here.
SKU_DAILY_SQL = text(
    """
WITH o AS (
    SELECT
        o.day, o.product_id,
        count(*)                                                      AS orders,
        sum(o.quantity)::bigint                                       AS units,
        sum(rev.v)::bigint                                            AS revenue_cents,
        sum(o.quantity::bigint * p.cost_cents)::bigint                AS cogs_cents,
        sum((rev.v * o.commission_bps + 5000) / 10000)::bigint        AS commission_cents,
        sum(o.logistics_cents::bigint)::bigint                        AS logistics_cents
    FROM orders o
    JOIN products p ON p.id = o.product_id
    CROSS JOIN LATERAL (
        SELECT o.quantity::bigint * o.unit_price_cents - o.discount_cents AS v
    ) rev
    WHERE o.seller_id = :seller_id
      AND o.day BETWEEN :date_from AND :date_to
      AND o.status = 'delivered'
    GROUP BY o.day, o.product_id
),
r AS (
    SELECT
        r.day, o.product_id,
        count(*)                                                      AS returns_count,
        sum(r.refund_cents::bigint)::bigint                           AS refunds_cents,
        sum((r.refund_cents::bigint * o.commission_bps + 5000) / 10000)::bigint
                                                                      AS commission_refund_cents,
        sum(r.return_logistics_cents::bigint)::bigint                 AS return_logistics_cents,
        sum(CASE WHEN r.restockable THEN r.quantity::bigint * p.cost_cents ELSE 0 END)::bigint
                                                                      AS restocked_cogs_cents
    FROM returns r
    JOIN orders o   ON o.id = r.order_id
    JOIN products p ON p.id = o.product_id
    WHERE r.seller_id = :seller_id
      AND r.day BETWEEN :date_from AND :date_to
      AND o.status = 'delivered'
    GROUP BY r.day, o.product_id
)
SELECT
    coalesce(o.day, r.day)                 AS day,
    coalesce(o.product_id, r.product_id)   AS product_id,
    coalesce(o.orders, 0)                  AS orders,
    coalesce(o.units, 0)                   AS units,
    coalesce(o.revenue_cents, 0)           AS revenue_cents,
    coalesce(o.cogs_cents, 0)              AS cogs_cents,
    coalesce(o.commission_cents, 0)        AS commission_cents,
    coalesce(o.logistics_cents, 0)         AS logistics_cents,
    coalesce(r.returns_count, 0)           AS returns_count,
    coalesce(r.refunds_cents, 0)           AS refunds_cents,
    coalesce(r.commission_refund_cents, 0) AS commission_refund_cents,
    coalesce(r.return_logistics_cents, 0)  AS return_logistics_cents,
    coalesce(r.restocked_cogs_cents, 0)    AS restocked_cogs_cents
FROM o
FULL JOIN r ON r.day = o.day AND r.product_id = o.product_id
"""
)
