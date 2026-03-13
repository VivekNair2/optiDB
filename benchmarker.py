import psycopg
import re
import json
import statistics
from datetime import datetime
import logging

logger = logging.getLogger("optidb_app")


def get_connection(db_host):
    """Create a database connection."""
    conn_info = f"host={db_host} port=5432 dbname=unoptimized_db user=ai_user password=secret options='-c statement_timeout=30000'"
    return psycopg.connect(conn_info, autocommit=True)


def split_ddl_and_query(sql: str):
    """Split a block that may start with DDL (CREATE INDEX / DROP INDEX etc.)
    from the final SELECT statement to benchmark.

    Returns (ddl_statements: list[str], select_query: str).
    """
    # Remove SQL line comments first, then split on semicolons
    sql_no_comments = re.sub(r"--[^\n]*", "", sql)

    ddl = []
    select = ""

    for stmt in sql_no_comments.split(";"):
        s = stmt.strip()
        if not s:
            continue
        first_word = s.split()[0].upper()
        if first_word in ("CREATE", "DROP", "ALTER", "TRUNCATE"):
            ddl.append(s + ";")
        elif first_word in ("SELECT", "WITH"):
            select = s  # last SELECT/CTE wins

    return ddl, select


def run_explain_analyze(conn, query):
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) on a query and return the plan."""
    clean_query = query.strip().rstrip(";")
    explain_query = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {clean_query}"
    with conn.cursor() as cur:
        cur.execute(explain_query)
        result = cur.fetchone()
        plan_data = result[0]
        # psycopg3 may return JSON as a string or a pre-decoded object
        if isinstance(plan_data, str):
            plan_data = json.loads(plan_data)
        if isinstance(plan_data, list):
            plan_data = plan_data[0]
        return plan_data


def parse_plan_metrics(plan):
    """Extract key metrics from an EXPLAIN ANALYZE JSON plan."""
    top = plan.get("Plan", {})

    def collect_nodes(node):
        """Recursively collect all plan nodes."""
        nodes = [node]
        for child in node.get("Plans", []):
            nodes.extend(collect_nodes(child))
        return nodes

    all_nodes = collect_nodes(top)

    # Check for sequential scans
    seq_scans = [n for n in all_nodes if n.get("Node Type") == "Seq Scan"]
    index_scans = [n for n in all_nodes if "Index" in n.get("Node Type", "")]

    # Shared buffers
    shared_hit = sum(n.get("Shared Hit Blocks", 0) for n in all_nodes)
    shared_read = sum(n.get("Shared Read Blocks", 0) for n in all_nodes)

    # Total rows removed by filter
    rows_removed = sum(n.get("Rows Removed by Filter", 0) for n in all_nodes)

    def _index_scan_label(n):
        # "Index Scan" / "Bitmap Heap Scan" have Relation Name
        # "Bitmap Index Scan" has only Index Name — derive table from index name
        if n.get("Relation Name"):
            return n["Relation Name"]
        idx = n.get("Index Name", "")
        # strip common prefixes like idx_tablename_col → tablename
        parts = idx.lstrip("idx_").split("_")
        return parts[0] if parts else idx

    return {
        "planning_time_ms": plan.get("Planning Time", 0),
        "execution_time_ms": plan.get("Execution Time", 0),
        "total_time_ms": plan.get("Planning Time", 0) + plan.get("Execution Time", 0),
        "total_cost": top.get("Total Cost", 0),
        "startup_cost": top.get("Startup Cost", 0),
        "actual_rows": top.get("Actual Rows", 0),
        "actual_loops": top.get("Actual Loops", 1),
        "plan_rows": top.get("Plan Rows", 0),
        "node_type": top.get("Node Type", "Unknown"),
        "seq_scan_count": len(seq_scans),
        "index_scan_count": len(index_scans),
        "seq_scan_tables": [n.get("Relation Name", "?") for n in seq_scans],
        "index_scan_tables": [_index_scan_label(n) for n in index_scans],
        "shared_hit_blocks": shared_hit,
        "shared_read_blocks": shared_read,
        "rows_removed_by_filter": rows_removed,
    }


def benchmark_query(db_host, query, runs=5, warmup=2):
    """Benchmark a single query (optionally prefixed with DDL) with warmup and multiple runs.

    If the query block contains DDL statements (CREATE INDEX etc.) they are executed
    once before benchmarking begins.  Only the final SELECT is passed to EXPLAIN ANALYZE.
    Returns dict with averaged metrics and individual run data.
    """
    ddl_stmts, select_query = split_ddl_and_query(query)

    if not select_query:
        return {"error": "No SELECT statement found to benchmark"}

    conn_info = f"host={db_host} port=5432 dbname=unoptimized_db user=ai_user password=secret options='-c statement_timeout=30000'"
    applied_ddl = []

    # --- Step 1: apply DDL on its own dedicated connection -----------------
    if ddl_stmts:
        logger.info(
            "Applying DDL statements before benchmarking: %d statement(s)",
            len(ddl_stmts),
        )
        try:
            with psycopg.connect(conn_info, autocommit=True) as ddl_conn:
                with ddl_conn.cursor() as cur:
                    for stmt in ddl_stmts:
                        try:
                            cur.execute(stmt)
                            applied_ddl.append(stmt)
                        except Exception as e:
                            if "already exists" in str(e).lower():
                                applied_ddl.append(
                                    f"(skipped — already exists): {stmt[:80]}"
                                )
                            else:
                                return {
                                    "error": f"DDL failed: {e}",
                                    "ddl_statement": stmt,
                                }
        except Exception as e:
            return {"error": f"DDL connection error: {e}"}

    # --- Step 2: benchmark on a *fresh* connection (no DDL residue) ---------
    results = []
    logger.info("Starting benchmark (Runs: %d, Warmups: %d)", runs, warmup)
    try:
        with psycopg.connect(conn_info, autocommit=True) as bench_conn:
            # Warmup runs (not counted)
            for _ in range(warmup):
                try:
                    run_explain_analyze(bench_conn, select_query)
                except Exception as e:
                    logger.warning("Warmup run failed: %s", e)
                    pass

            # Measured runs
            for i in range(runs):
                try:
                    plan = run_explain_analyze(bench_conn, select_query)
                    metrics = parse_plan_metrics(plan)
                    metrics["run_number"] = i + 1
                    results.append(metrics)
                except Exception as e:
                    logger.error("Benchmark run %d failed: %s", i + 1, e)
                    results.append({"error": str(e), "run_number": i + 1})
    except Exception as e:
        logger.error("Benchmark connection error: %s", e)
        return {"error": f"Benchmark connection error: {e}"}

    # Filter successful runs
    successful = [r for r in results if "error" not in r]

    if not successful:
        return {
            "error": "All benchmark runs failed",
            "details": results,
        }

    # Compute averages
    avg_metrics = {
        "planning_time_ms": statistics.mean(
            [r["planning_time_ms"] for r in successful]
        ),
        "execution_time_ms": statistics.mean(
            [r["execution_time_ms"] for r in successful]
        ),
        "total_time_ms": statistics.mean([r["total_time_ms"] for r in successful]),
        "total_cost": successful[0]["total_cost"],
        "startup_cost": successful[0]["startup_cost"],
        "actual_rows": successful[0]["actual_rows"],
        "plan_rows": successful[0]["plan_rows"],
        "node_type": successful[0]["node_type"],
        "seq_scan_count": successful[0]["seq_scan_count"],
        "index_scan_count": successful[0]["index_scan_count"],
        "seq_scan_tables": successful[0]["seq_scan_tables"],
        "index_scan_tables": successful[0]["index_scan_tables"],
        "shared_hit_blocks": successful[0]["shared_hit_blocks"],
        "shared_read_blocks": successful[0]["shared_read_blocks"],
        "rows_removed_by_filter": successful[0]["rows_removed_by_filter"],
        "runs": runs,
        "warmup": warmup,
        "successful_runs": len(successful),
        "individual_runs": results,
        "applied_ddl": applied_ddl,
    }

    # Add min/max/stddev for timing
    exec_times = [r["execution_time_ms"] for r in successful]
    if len(exec_times) > 1:
        avg_metrics["execution_time_stddev"] = statistics.stdev(exec_times)
        avg_metrics["execution_time_min"] = min(exec_times)
        avg_metrics["execution_time_max"] = max(exec_times)

    return avg_metrics


def _drop_optimization_indexes(db_host, optimized_query):
    """Drop any indexes or materialized views that the optimized query would CREATE,
    so the original query can be benchmarked in a clean (pre-optimization) state."""
    ddl_stmts, _ = split_ddl_and_query(optimized_query)
    if not ddl_stmts:
        return

    drop_stmts = []
    for stmt in ddl_stmts:
        # Match: CREATE [UNIQUE] INDEX ...
        m_idx = re.search(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON",
            stmt,
            re.IGNORECASE,
        )
        if m_idx:
            drop_stmts.append(f"DROP INDEX IF EXISTS {m_idx.group(1)};")

        # Match: CREATE MATERIALIZED VIEW ...
        m_mv = re.search(
            r"CREATE\s+(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
            stmt,
            re.IGNORECASE,
        )
        if m_mv:
            is_mat = "MATERIALIZED" in stmt.upper()
            mat_str = "MATERIALIZED " if is_mat else ""
            drop_stmts.append(f"DROP {mat_str}VIEW IF EXISTS {m_mv.group(1)} CASCADE;")

    if not drop_stmts:
        return

    conn_info = f"host={db_host} port=5432 dbname=unoptimized_db user=ai_user password=secret options='-c statement_timeout=30000'"
    with psycopg.connect(conn_info, autocommit=True) as conn:
        with conn.cursor() as cur:
            for stmt in drop_stmts:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass


def compare_queries(db_host, original_query, optimized_query, runs=5, warmup=2):
    """Run benchmarks on both queries and return comparison data."""
    logger.info("Initializing query comparison: Original vs Optimized")
    # Ensure original runs without the optimization indexes (fair baseline)
    _drop_optimization_indexes(db_host, optimized_query)

    logger.info("Benchmarking ORIGINAL query...")
    original_result = benchmark_query(db_host, original_query, runs, warmup)

    logger.info("Benchmarking OPTIMIZED query...")
    optimized_result = benchmark_query(db_host, optimized_query, runs, warmup)

    comparison = {
        "original": original_result,
        "optimized": optimized_result,
        "timestamp": datetime.now().isoformat(),
        "config": {"runs": runs, "warmup": warmup},
    }

    # Compute improvements (only if both succeeded)
    if "error" not in original_result and "error" not in optimized_result:
        orig_exec = original_result["execution_time_ms"]
        opt_exec = optimized_result["execution_time_ms"]
        orig_cost = original_result["total_cost"]
        opt_cost = optimized_result["total_cost"]

        comparison["improvements"] = {
            "execution_time_pct": (
                ((orig_exec - opt_exec) / orig_exec * 100) if orig_exec > 0 else 0
            ),
            "cost_reduction_pct": (
                ((orig_cost - opt_cost) / orig_cost * 100) if orig_cost > 0 else 0
            ),
            "seq_scans_removed": original_result["seq_scan_count"]
            - optimized_result["seq_scan_count"],
            "index_scans_added": optimized_result["index_scan_count"]
            - original_result["index_scan_count"],
            "buffer_hit_improvement": optimized_result["shared_hit_blocks"]
            - original_result["shared_hit_blocks"],
        }

    return comparison


def format_benchmark_report(comparison):
    """Format a comparison into a readable Markdown report for DBA review."""
    lines = []
    lines.append("# Query Benchmark Report")
    lines.append(f"**Generated:** {comparison['timestamp']}")
    lines.append(
        f"**Methodology:** EXPLAIN (ANALYZE, BUFFERS) with {comparison['config']['runs']} measured runs, {comparison['config']['warmup']} warmup runs"
    )
    lines.append("")

    orig = comparison["original"]
    opt = comparison["optimized"]

    # The actual "timeout" string might be hiding inside the "details" array
    # instead of the main "error" string (which usually says "All benchmark runs failed").
    orig_error_text = str(orig.get("error", "")) + " " + str(orig.get("details", ""))
    orig_timeout = "timeout" in orig_error_text.lower() if "error" in orig else False

    # Show DDL applied before optimized benchmark early so it's always visible
    applied_ddl = opt.get("applied_ddl", [])
    if applied_ddl:
        lines.append("## Setup: Indexes Applied Before Benchmarking")
        lines.append("")
        for stmt in applied_ddl:
            lines.append(f"```sql\n{stmt}\n```")
        lines.append("")

    if "error" in orig and not orig_timeout:
        lines.append("### ❌ Original Query Failed (Execution Error)")
        lines.append(f"> `{orig['error']}`")
        if "details" in orig:
            run_errors = [r.get("error") for r in orig["details"] if "error" in r]
            if run_errors:
                lines.append(f"**Exception Details:** `{run_errors[0]}`")
        return "\n".join(lines)

    if "error" in opt:
        lines.append("### ❌ Optimized Query Failed (Execution Error)")
        lines.append(f"> `{opt['error']}`")
        if "details" in opt:
            run_errors = [r.get("error") for r in opt["details"] if "error" in r]
            if run_errors:
                lines.append(f"**Exception Details:** `{run_errors[0]}`")
        return "\n".join(lines)

    if orig_timeout:
        lines.append("## 🚨 Timeout Analysis")
        lines.append(
            "> The **Original Query** was severely unoptimized and exceeded the database safety circuit-breaker of **30 seconds**. This indicates a catastrophic execution plan (endless nested loops or massive unindexed scans) that would typically lock up the entire production database."
        )
        lines.append("")
        lines.append("### 🏆 Optimizer Results")
        lines.append(
            f"The optimized query processed successfully in **{opt.get('execution_time_ms', 0):.2f} ms**!"
        )
        lines.append("")
        lines.append("| Metric | Status |")
        lines.append("|--------|--------|")
        lines.append(
            f"| **Execution Time** | {opt.get('execution_time_ms', 0):.3f} ms |"
        )
        lines.append(f"| **Estimated Cost** | {opt.get('total_cost', 0):.2f} |")
        lines.append(f"| **Rows Returned**  | {opt.get('actual_rows', 0)} |")
        lines.append(f"| **Sequential Scans** | {opt.get('seq_scan_count', 0)} |")
        lines.append(f"| **Index Scans** | {opt.get('index_scan_count', 0)} |")
        if opt.get("index_scan_tables"):
            lines.append("")
            lines.append(
                f"**Indexes Leveraged:** {', '.join(opt['index_scan_tables'])}"
            )
        return "\n".join(lines)

    # Summary table
    lines.append("## Performance Comparison")
    lines.append("")
    lines.append("| Metric | Original | Optimized | Change |")
    lines.append("|--------|----------|-----------|--------|")

    # Execution time
    orig_exec = orig["execution_time_ms"]
    opt_exec = opt["execution_time_ms"]
    exec_change = comparison["improvements"]["execution_time_pct"]
    exec_arrow = "faster" if exec_change > 0 else "slower"
    lines.append(
        f"| **Execution Time** | {orig_exec:.3f} ms | {opt_exec:.3f} ms | {abs(exec_change):.1f}% {exec_arrow} |"
    )

    # Planning time
    lines.append(
        f"| **Planning Time** | {orig['planning_time_ms']:.3f} ms | {opt['planning_time_ms']:.3f} ms | - |"
    )

    # Total time
    orig_total = orig["total_time_ms"]
    opt_total = opt["total_time_ms"]
    total_change = (
        ((orig_total - opt_total) / orig_total * 100) if orig_total > 0 else 0
    )
    lines.append(
        f"| **Total Time** | {orig_total:.3f} ms | {opt_total:.3f} ms | {abs(total_change):.1f}% {'faster' if total_change > 0 else 'slower'} |"
    )

    # Cost
    cost_change = comparison["improvements"]["cost_reduction_pct"]
    lines.append(
        f"| **Estimated Cost** | {orig['total_cost']:.2f} | {opt['total_cost']:.2f} | {abs(cost_change):.1f}% {'lower' if cost_change > 0 else 'higher'} |"
    )

    # Rows
    lines.append(
        f"| **Rows Returned** | {orig['actual_rows']} | {opt['actual_rows']} | - |"
    )

    # Scan types
    lines.append(
        f"| **Sequential Scans** | {orig['seq_scan_count']} | {opt['seq_scan_count']} | - |"
    )
    lines.append(
        f"| **Index Scans** | {orig['index_scan_count']} | {opt['index_scan_count']} | - |"
    )

    # Buffer usage
    lines.append(
        f"| **Buffer Hits** | {orig['shared_hit_blocks']} | {opt['shared_hit_blocks']} | - |"
    )
    lines.append(
        f"| **Buffer Reads (disk)** | {orig['shared_read_blocks']} | {opt['shared_read_blocks']} | - |"
    )

    # Rows filtered
    lines.append(
        f"| **Rows Removed by Filter** | {orig['rows_removed_by_filter']} | {opt['rows_removed_by_filter']} | - |"
    )

    lines.append("")

    # Execution time consistency
    if "execution_time_min" in orig:
        lines.append("## Execution Time Distribution")
        lines.append("")
        lines.append("| Stat | Original | Optimized |")
        lines.append("|------|----------|-----------|")
        lines.append(
            f"| Min | {orig.get('execution_time_min', 0):.3f} ms | {opt.get('execution_time_min', 0):.3f} ms |"
        )
        lines.append(
            f"| Max | {orig.get('execution_time_max', 0):.3f} ms | {opt.get('execution_time_max', 0):.3f} ms |"
        )
        lines.append(
            f"| Avg | {orig['execution_time_ms']:.3f} ms | {opt['execution_time_ms']:.3f} ms |"
        )
        lines.append(
            f"| Std Dev | {orig.get('execution_time_stddev', 0):.3f} ms | {opt.get('execution_time_stddev', 0):.3f} ms |"
        )
        lines.append("")

    # Scan details
    lines.append("## Scan Analysis")
    lines.append("")
    if orig["seq_scan_tables"]:
        lines.append(
            f"**Original - Sequential Scans on:** {', '.join(orig['seq_scan_tables'])}"
        )
    if orig["index_scan_tables"]:
        lines.append(
            f"**Original - Index Scans on:** {', '.join(orig['index_scan_tables'])}"
        )
    if opt["seq_scan_tables"]:
        lines.append(
            f"**Optimized - Sequential Scans on:** {', '.join(opt['seq_scan_tables'])}"
        )
    if opt["index_scan_tables"]:
        lines.append(
            f"**Optimized - Index Scans on:** {', '.join(opt['index_scan_tables'])}"
        )
    lines.append("")

    # Verdict
    lines.append("## Recommendation")
    lines.append("")
    if exec_change > 10:
        lines.append(
            f"The optimized query is **{exec_change:.1f}% faster** with a cost reduction of **{cost_change:.1f}%**. **Recommended to adopt the optimized query.**"
        )
    elif exec_change > 0:
        lines.append(
            f"The optimized query shows a marginal improvement of **{exec_change:.1f}%**. Consider adopting if the query runs frequently."
        )
    elif exec_change == 0:
        lines.append(
            "Both queries perform similarly. Review the query plan for structural improvements."
        )
    else:
        lines.append(
            f"The optimized query is **{abs(exec_change):.1f}% slower**. The original query may already be well-optimized, or the optimization doesn't suit the current data distribution. **Keep the original query.**"
        )

    return "\n".join(lines)
