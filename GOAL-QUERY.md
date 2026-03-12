# Goal and Query Bank for Strong Benchmark Demos

This file collects SQL goals and queries that are good candidates for the Query Optimizer tab.
They are designed to produce a visible raw-vs-optimized difference on the bundled TPC-H SF 0.1 dataset.

## Benchmark Hygiene

Use these queries on a clean database state for the fairest comparison.

- If you already ran an optimized version once, the created indexes may still exist.
- Recreate the database with `docker compose down -v` and then `docker compose up -d --build`, or manually drop any generated indexes before re-running the raw query.
- The bundled TPC-H setup already creates these baseline indexes:
  - `lineitem(l_shipdate)`, `lineitem(l_orderkey)`, `lineitem(l_partkey)`, `lineitem(l_suppkey)`, `lineitem(l_returnflag, l_linestatus)`
  - `orders(o_custkey)`, `orders(o_orderdate)`, `orders(o_orderstatus)`
  - `customer(c_nationkey)`, `customer(c_mktsegment)`
  - `supplier(s_nationkey)`
  - `partsupp(ps_suppkey)`
  - `part(p_type)`, `part(p_mfgr)`

The queries below deliberately lean on other columns such as `l_receiptdate`, `l_commitdate`, `l_shipmode`, `l_quantity`, `l_discount`, `o_totalprice`, `o_orderpriority`, `c_acctbal`, `p_brand`, `p_container`, `p_size`, `ps_availqty`, and `ps_supplycost`.

## Good Benchmark Queries

### 1. Goal: Find late, high-volume shipments by shipping mode in Q3 1995

Why this is useful:
- Filters a large slice of `lineitem`
- Uses `l_receiptdate`, `l_shipmode`, and `l_quantity`, which are good candidates for new indexes

```sql
SELECT
	l.l_shipmode,
	COUNT(*) AS shipment_count,
	SUM(l.l_extendedprice * (1 - l.l_discount)) AS revenue
FROM lineitem l
WHERE l.l_receiptdate >= DATE '1995-07-01'
  AND l.l_receiptdate < DATE '1995-10-01'
  AND l.l_shipmode IN ('MAIL', 'SHIP')
  AND l.l_quantity > 25
GROUP BY l.l_shipmode
ORDER BY revenue DESC;
```

### 2. Goal: Measure discount leakage on expensive urgent orders

Why this is useful:
- Combines large-table filters across both `orders` and `lineitem`
- Likely to benefit from indexes on `o_totalprice`, `o_orderpriority`, `l_receiptdate`, and `l_discount`

```sql
SELECT
	o.o_orderpriority,
	COUNT(DISTINCT o.o_orderkey) AS order_count,
	SUM(l.l_extendedprice * l.l_discount) AS discount_loss
FROM orders o
JOIN lineitem l
	ON o.o_orderkey = l.l_orderkey
WHERE o.o_totalprice > 200000
  AND o.o_orderpriority IN ('1-URGENT', '2-HIGH')
  AND l.l_receiptdate >= DATE '1995-01-01'
  AND l.l_receiptdate < DATE '1996-01-01'
  AND l.l_discount BETWEEN 0.05 AND 0.08
GROUP BY o.o_orderpriority
ORDER BY discount_loss DESC;
```

### 3. Goal: Find premium customers in the BUILDING segment placing very high-value priority orders

Why this is useful:
- `c_mktsegment` already has an index, but `c_acctbal`, `o_totalprice`, and `o_orderpriority` do not
- Good example where the raw query is only partially helped by existing indexes

```sql
SELECT
	c.c_name,
	COUNT(*) AS order_count,
	SUM(o.o_totalprice) AS total_value
FROM customer c
JOIN orders o
	ON c.c_custkey = o.o_custkey
WHERE c.c_mktsegment = 'BUILDING'
  AND c.c_acctbal > 7000
  AND o.o_totalprice > 250000
  AND o.o_orderpriority IN ('1-URGENT', '2-HIGH')
GROUP BY c.c_name
ORDER BY total_value DESC
LIMIT 20;
```

### 4. Goal: Analyze returned-item discount exposure by receipt month

Why this is useful:
- Stays on the 600K-row `lineitem` table
- Good candidate for filters on `l_receiptdate` and `l_discount`

```sql
SELECT
	date_trunc('month', l.l_receiptdate) AS receipt_month,
	l.l_returnflag,
	COUNT(*) AS line_count,
	SUM(l.l_extendedprice * l.l_discount) AS discount_value
FROM lineitem l
WHERE l.l_receiptdate >= DATE '1994-01-01'
  AND l.l_receiptdate < DATE '1996-01-01'
  AND l.l_discount BETWEEN 0.06 AND 0.08
  AND l.l_returnflag = 'R'
GROUP BY 1, 2
ORDER BY 1, 2;
```

## Harder Queries with Larger Expected Benchmark Gaps

These are more complex and usually have a higher chance of showing a larger benchmark delta because they combine multiple joins, filters, and aggregations over unindexed columns.

### 5. Goal: Identify suppliers generating the most revenue from late deliveries on expensive fulfilled orders

Why this is useful:
- Joins `supplier`, `nation`, `partsupp`, `lineitem`, and `orders`
- Likely to benefit from new indexes on `l_receiptdate`, `l_discount`, `ps_supplycost`, and `o_totalprice`

```sql
SELECT
	s.s_name,
	n.n_name,
	COUNT(*) AS late_lines,
	SUM(l.l_extendedprice * (1 - l.l_discount)) AS net_revenue,
	AVG(ps.ps_supplycost) AS avg_supply_cost
FROM supplier s
JOIN nation n
	ON s.s_nationkey = n.n_nationkey
JOIN partsupp ps
	ON s.s_suppkey = ps.ps_suppkey
JOIN lineitem l
	ON ps.ps_partkey = l.l_partkey
   AND ps.ps_suppkey = l.l_suppkey
JOIN orders o
	ON l.l_orderkey = o.o_orderkey
WHERE l.l_receiptdate > l.l_commitdate
  AND l.l_receiptdate >= DATE '1995-01-01'
  AND l.l_receiptdate < DATE '1996-01-01'
  AND l.l_discount BETWEEN 0.04 AND 0.07
  AND ps.ps_supplycost > 500
  AND o.o_totalprice > 150000
GROUP BY s.s_name, n.n_name
ORDER BY net_revenue DESC
LIMIT 20;
```

### 6. Goal: Find bulky product mixes that caused costly air shipments

Why this is useful:
- Good stress test across `part`, `lineitem`, and `orders`
- Targets unindexed product attributes like `p_brand`, `p_container`, and `p_size`

```sql
SELECT
	p.p_brand,
	p.p_container,
	p.p_size,
	COUNT(*) AS line_count,
	SUM(l.l_extendedprice) AS gross_revenue
FROM part p
JOIN lineitem l
	ON p.p_partkey = l.l_partkey
JOIN orders o
	ON l.l_orderkey = o.o_orderkey
WHERE p.p_brand IN ('Brand#12', 'Brand#23', 'Brand#34')
  AND p.p_container IN ('SM BOX', 'MED BOX', 'LG BOX')
  AND p.p_size BETWEEN 10 AND 30
  AND l.l_shipmode IN ('AIR', 'AIR REG')
  AND l.l_receiptdate > l.l_commitdate
  AND o.o_totalprice > 100000
GROUP BY p.p_brand, p.p_container, p.p_size
ORDER BY gross_revenue DESC, line_count DESC;
```

### 7. Goal: Find low-balance customers who still drove high-margin urgent orders

Why this is useful:
- Combines selective customer, order, and shipment filters that currently lack composite access paths
- Often produces a larger gap because both `orders` and `lineitem` need help

```sql
SELECT
	c.c_name,
	c.c_acctbal,
	COUNT(DISTINCT o.o_orderkey) AS urgent_orders,
	SUM(l.l_extendedprice * (1 - l.l_discount)) AS net_revenue
FROM customer c
JOIN orders o
	ON c.c_custkey = o.o_custkey
JOIN lineitem l
	ON o.o_orderkey = l.l_orderkey
WHERE c.c_acctbal < 0
  AND o.o_totalprice > 175000
  AND o.o_orderpriority IN ('1-URGENT', '2-HIGH')
  AND l.l_shipmode IN ('AIR', 'AIR REG')
  AND l.l_discount < 0.04
GROUP BY c.c_name, c.c_acctbal
HAVING COUNT(DISTINCT o.o_orderkey) >= 3
ORDER BY net_revenue DESC
LIMIT 25;
```

### 8. Goal: Detect costly low-stock parts that keep appearing in delayed large-quantity shipments

Why this is useful:
- Strong candidate for indexes on `partsupp(ps_availqty, ps_supplycost)` and `lineitem(l_receiptdate, l_quantity)`
- Useful demo because it hits several unindexed business attributes at once

```sql
SELECT
	p.p_name,
	s.s_name,
	ps.ps_availqty,
	COUNT(*) AS demand_lines,
	SUM(l.l_quantity) AS total_quantity,
	SUM(l.l_extendedprice) AS gross_revenue
FROM part p
JOIN partsupp ps
	ON p.p_partkey = ps.ps_partkey
JOIN supplier s
	ON ps.ps_suppkey = s.s_suppkey
JOIN lineitem l
	ON ps.ps_partkey = l.l_partkey
   AND ps.ps_suppkey = l.l_suppkey
WHERE p.p_brand = 'Brand#23'
  AND p.p_container IN ('MED BAG', 'MED BOX')
  AND ps.ps_availqty < 300
  AND ps.ps_supplycost > 400
  AND l.l_receiptdate > l.l_commitdate
  AND l.l_quantity > 30
GROUP BY p.p_name, s.s_name, ps.ps_availqty
ORDER BY total_quantity DESC, gross_revenue DESC
LIMIT 20;
```

## How to Use These in the App

1. Paste the goal text into the task box.
2. Paste the SQL into the Query Optimizer tab.
3. Click `Analyze & Optimize`.
4. Review the generated `CREATE INDEX` statements and rewritten query.
5. Run `Execute Optimized`.
6. Run `Benchmark` to compare raw vs optimized plans on the TPC-H dataset.

If a benchmark shows only a tiny difference, it usually means one of these happened:

- The optimizer chose a query that still overlaps heavily with existing baseline indexes.
- A previously-created index is still present from an earlier run.
- The raw and optimized queries ended up using nearly the same plan.

For the biggest before/after demos, queries 5 through 8 are usually the better picks.
