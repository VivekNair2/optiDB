# Database Optimization: Hypothesis Testing Framework

To rigorously prove the value of your AI Database Optimizer tool, you should test specific hypotheses about database performance execution. 

Below are 4 distinct hypotheses to test in your project, each targeting a specific relational database anti-pattern. 

---

## Hypothesis 1: The SARGability Principle
**Hypothesis:** Applying functions to indexed columns prevents the database query planner from using an index (forcing a Sequential Scan). Rewriting the query to a SARGable (Search Argument Able) format and providing a B-Tree index will reduce execution time by over 90%.

**Task / Goal (For the AI Input):**
"Find the total revenue and count of orders grouped by order priority, but only for orders placed in March 1996."

**The Unoptimized Query:**
```sql
SELECT 
    o_orderpriority, 
    COUNT(*) as order_count,
    SUM(o_totalprice) as total_revenue
FROM orders
WHERE EXTRACT(YEAR FROM o_orderdate) = 1996 
  AND EXTRACT(MONTH FROM o_orderdate) = 3
GROUP BY o_orderpriority
ORDER BY total_revenue DESC;
```
* **What to prove in the App:** Put this in the Optimizer. It will rewrite `EXTRACT()` into `o_orderdate >= '1996-03-01' AND o_orderdate < '1996-04-01'`. The benchmark will show the original doing a massive Sequential Scan, while the optimized query performs a highly efficient Index Scan.

---

## Hypothesis 2: The Correlated Subquery Trap
**Hypothesis:** Correlated subqueries inside a `WHERE` clause force the database to re-evaluate the subquery for every single row returned by the outer query (Nested Loops). Flattening this into a CTE, Window Function, or `JOIN` will shift the planner to a single Hash Join, drastically reducing total cost.

**Task / Goal (For the AI Input):**
"List the names, quantities, and extended prices of parts, but only include line items where the extended price is strictly higher than the average extended price for that specific part."

**The Unoptimized Query:**
```sql
SELECT 
    p.p_name, 
    l.l_quantity, 
    l.l_extendedprice
FROM part p
JOIN lineitem l ON p.p_partkey = l.l_partkey
WHERE l.l_extendedprice > (
    SELECT AVG(l2.l_extendedprice)
    FROM lineitem l2
    WHERE l2.l_partkey = p.p_partkey
)
LIMIT 100;
```
* **What to prove in the App:** The original query will time out or take a very long time because it calculates the average price millions of times. The AI will rewrite it to pre-calculate the averages once (via CTE or MV). The benchmark will show a complete elimination of the catastrophic nested loop.

---

## Hypothesis 3: High-Cost Aggregation via Materialized Views
**Hypothesis:** Queries that perform heavy `GROUP BY` aggregations across multiple large tables require massive CPU and Memory during read-time. By shifting this aggregation to write-time via a `MATERIALIZED VIEW`, read execution time can be reduced from seconds/minutes to sub-milliseconds.

**Task / Goal (For the AI Input):**
"Calculate the total net revenue for each customer market segment and nation based on orders placed between 1994 and 1996."

**The Unoptimized Query:**
```sql
SELECT 
    c.c_mktsegment, 
    n.n_name,
    SUM(l.l_extendedprice * (1 - l.l_discount)) as net_revenue
FROM customer c
JOIN nation n ON c.c_nationkey = n.n_nationkey
JOIN orders o ON c.c_custkey = o.o_custkey
JOIN lineitem l ON o.o_orderkey = l.l_orderkey
WHERE o.o_orderdate BETWEEN '1994-01-01' AND '1996-12-31'
GROUP BY c.c_mktsegment, n.n_name
ORDER BY net_revenue DESC;
```
* **What to prove in the App:** The AI should suggest a `MATERIALIZED VIEW` that pre-calculates revenue by segment and nation. The benchmark will prove that querying the Materialized View bypasses 4 heavy table joins and a massive runtime aggregation.

---

## Hypothesis 4: `IN` vs. `EXISTS` on Large Datasets
**Hypothesis:** Using `IN` with a subquery on large datasets forces the database to materialize the entire subquery result into memory before filtering. Rewriting to `EXISTS` or an explicit `JOIN` allows the planner to employ short-circuit evaluations and semi-joins, dropping buffer reads significantly.

**Task / Goal (For the AI Input):**
"Get the order keys, total prices, and order dates for all orders that have at least one line item shipped by 'AIR' where the commit date was earlier than the receipt date."

**The Unoptimized Query:**
```sql
SELECT 
    o.o_orderkey, 
    o.o_totalprice, 
    o.o_orderdate
FROM orders o
WHERE o.o_orderkey IN (
    SELECT l_orderkey
    FROM lineitem
    WHERE l_shipmode = 'AIR'
      AND l_commitdate < l_receiptdate
);
```
* **What to prove in the App:** The optimizer will replace the `IN (SELECT...)` with an `EXISTS` block or an `INNER JOIN`. Look specifically at the "Buffer Read/Hit improvement" metric in the benchmark report—the memory footprint (Shared Hit Blocks) will be substantially lower.
