import streamlit as st
from dotenv import load_dotenv
import os
import re
import psycopg
import pandas as pd
from workload import (
    get_query_workload,
    get_schema_summary,
    get_existing_indexes,
    execute_ddl,
    reset_stats,
)
from agents import (
    optimizer_agent as workload_optimizer_agent,
    rewriter_agent,
    OptimizationPlan,
)
from agno.agent import Agent
from agno.tools.sql import SQLTools
from agno.models.openai import OpenAIChat
from benchmarker import compare_queries, format_benchmark_report, split_ddl_and_query

load_dotenv()

os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY", "")

db_host = os.getenv("DB_HOST", "postgres")
db_url = f"postgresql+psycopg://ai_user:secret@{db_host}:5432/unoptimized_db"


# ─── Helpers ──────────────────────────────────────────────────────────────────


def gather_query_context(sql: str) -> str:
    """Pre-fetch schema, workload, pg_indexes and EXPLAIN ANALYZE as plain text
    for the optimizer prompt so the agent has full context."""
    conn_info = (
        f"host={db_host} port=5432 dbname=unoptimized_db user=ai_user password=secret"
    )
    lines = []

    # 1. Schema summary
    try:
        schema = get_schema_summary()
        lines.append("=== DATABASE SCHEMA ===")
        lines.append(schema)
    except Exception as e:
        lines.append(f"=== DATABASE SCHEMA ===\n  (failed: {e})")

    # 2. Recent workload from pg_stat_statements
    try:
        workload = get_query_workload()
        lines.append(
            "\n=== RECENT QUERY WORKLOAD (pg_stat_statements, top 5 slowest) ==="
        )
        if workload:
            for i, q in enumerate(workload[:5]):
                lines.append(
                    f"  #{i+1} avg={q['avg_ms']}ms calls={q['calls']}: {q['query'][:120].strip()}"
                )
        else:
            lines.append("  (no workload data yet)")
    except Exception as e:
        lines.append(f"\n=== RECENT QUERY WORKLOAD ===\n  (failed: {e})")

    # 3. All existing indexes
    try:
        with psycopg.connect(conn_info) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tablename, indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    ORDER BY tablename, indexname;
                """
                )
                rows = cur.fetchall()
                lines.append("\n=== EXISTING INDEXES (from pg_indexes) ===")
                if rows:
                    for tbl, idx, defn in rows:
                        lines.append(f"  {tbl}: {idx} — {defn}")
                else:
                    lines.append("  (none found)")

                # 4. EXPLAIN ANALYZE on the query
                lines.append("\n=== EXPLAIN (ANALYZE, BUFFERS) OUTPUT ===")
                try:
                    cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}")
                    for r in cur.fetchall():
                        lines.append(r[0])
                except Exception as e:
                    lines.append(f"  EXPLAIN failed: {e}")
    except Exception as e:
        lines.append(f"\nContext gathering failed: {e}")

    return "\n".join(lines)


def execute_sql_directly(sql: str):
    """Execute a SQL block (optionally prefixed with DDL) directly via psycopg.
    Applies DDL statements first, then runs the SELECT and returns (columns, rows, error).
    """
    conn_info = (
        f"host={db_host} port=5432 dbname=unoptimized_db user=ai_user password=secret"
    )
    ddl_stmts, select_query = split_ddl_and_query(sql)

    if not select_query:
        return None, None, "No SELECT statement found in the query."

    try:
        # Apply DDL first (CREATE INDEX etc.) with autocommit
        if ddl_stmts:
            with psycopg.connect(conn_info, autocommit=True) as conn:
                with conn.cursor() as cur:
                    for stmt in ddl_stmts:
                        try:
                            cur.execute(stmt)
                        except Exception as e:
                            if "already exists" not in str(e).lower():
                                return None, None, f"DDL failed: {e}\nStatement: {stmt}"

        # Run the SELECT
        with psycopg.connect(conn_info) as conn:
            with conn.cursor() as cur:
                cur.execute(select_query)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall()
                return cols, rows, None
    except Exception as e:
        return None, None, str(e)


def extract_optimized_sql(text: str) -> str:
    """Extract the full optimized SQL (DDL + SELECT) from agent output.

    Handles two LLM output styles:
    1. Everything in one ```sql``` block (preferred)
    2. CREATE INDEX in separate blocks followed by a SELECT block
    """
    matches = re.findall(r"```sql\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if not matches:
        # Fallback: plain-text patterns
        for pattern in [
            r"(?:Optimized Query|Optimized SQL):\s*((?:CREATE INDEX.*?;\s*)*SELECT.*?)(?:\n\n|Explanation|Recommended|\Z)",
            r"(?:Optimized Query|Optimized SQL):\s*(SELECT.*?)(?:\n\n|Explanation|Recommended|\Z)",
        ]:
            m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if m:
                return m.group(1).strip()
        bare = re.findall(r"(SELECT\s+.+?FROM.+?;)", text, re.DOTALL | re.IGNORECASE)
        return max(bare, key=len).strip() if bare else ""

    if len(matches) == 1:
        return matches[0].strip()

    # Multiple blocks — collect DDL blocks and the final SELECT block together.
    # Look for the section after "### Optimized Query" if present.
    section_match = re.search(
        r"###\s*Optimized Query.*?```sql\s*(.*)", text, re.DOTALL | re.IGNORECASE
    )
    if section_match:
        section_text = section_match.group(1)
        section_blocks = re.findall(r"(.*?)\s*```", section_text, re.DOTALL)
        combined = "\n".join(b.strip() for b in section_blocks if b.strip())
        if combined:
            return combined

    # Fall back: join all DDL blocks + last SELECT block
    ddl_blocks = [
        m.strip()
        for m in matches[:-1]
        if re.match(r"\s*(CREATE|DROP|ALTER)", m, re.IGNORECASE)
    ]
    select_block = matches[-1].strip()
    if ddl_blocks:
        return "\n\n".join(ddl_blocks) + "\n\n" + select_block
    return select_block


# ─── Agents for Query Optimizer tab ───────────────────────────────────────────

sql_optimizer_agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[SQLTools(db_url=db_url)],
    instructions=[
        "You are a senior PostgreSQL performance engineer.",
        "STEP 1 — UNDERSTAND THE SCHEMA: Use describe_table on every table in the query.",
        "STEP 2 — CHECK EXISTING INDEXES: Run:",
        "  SELECT indexname, tablename, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY tablename, indexname;",
        "  Do NOT assume any indexes exist — only trust what this query returns.",
        "STEP 3 — GET THE QUERY PLAN: Run EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) on the original query.",
        "STEP 4 — IDENTIFY REAL ISSUES based on the actual plan and actual indexes:",
        "  - Sequential scans on filtered columns with no index → CREATE INDEX",
        "  - Implicit comma joins → rewrite with explicit JOIN",
        "  - Non-sargable predicates → fix predicate",
        "  - SELECT * → list only needed columns",
        "STEP 5 — OUTPUT in this exact format:",
        "  ### Findings",
        "  ### Missing Indexes",
        "  ### Optimized Query",
        "  ```sql",
        "  <CREATE INDEX statements first, then the SELECT>",
        "  ```",
        "  ### Explanation",
        "CRITICAL: Include CREATE INDEX DDL inside the ```sql block before the SELECT.",
        "CRITICAL: Base analysis on actual pg_indexes output, not assumptions.",
    ],
    markdown=True,
)

executor_agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[SQLTools(db_url=db_url)],
    instructions=[
        "You are a SQL execution assistant.",
        "IMPORTANT: You MUST execute the SQL using the run_sql_query tool.",
        "Return actual rows as a markdown table.",
        "If no rows, say 'No results found'. If it fails, show the error.",
    ],
    markdown=True,
)


# ─── App layout ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="DB Optimizer Agent", page_icon="🧠", layout="wide")
st.title("🧠 AI Database Optimizer")
st.caption(
    "Analyzes query workload → suggests indexes & materialized views → waits for your approval before applying anything"
)
st.divider()

tab1, tab2, tab3, tab4 = st.tabs(
    [
        "📊 Workload Monitor",
        "🤖 Analyze & Approve",
        "🔧 Query Optimizer",
        "✍️ Query Rewriter",
    ]
)


# ─── Tab 1: Workload Monitor ──────────────────────────────────────────────────
with tab1:
    st.subheader("Query Workload from pg_stat_statements")
    st.caption("Tracks all queries run against the database since the last stats reset")

    col1, col2, col3 = st.columns([1, 1, 5])
    with col1:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
    with col2:
        if st.button(
            "🗑️ Reset Stats",
            use_container_width=True,
            help="Clears pg_stat_statements history",
        ):
            try:
                reset_stats()
                st.success("Stats reset!")
                st.rerun()
            except Exception as e:
                st.error(f"Reset failed: {e}")

    st.divider()

    try:
        workload = get_query_workload()
        if not workload:
            st.info(
                "No workload data yet. Run some queries against your database, then refresh."
            )
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("Queries Tracked", len(workload))
            m2.metric("Slowest Avg (ms)", max(q["avg_ms"] for q in workload))
            m3.metric("Total Calls", sum(q["calls"] for q in workload))
            st.divider()
            for q in workload:
                avg_ms = float(q["avg_ms"])
                icon = "🔴" if avg_ms > 100 else "🟡" if avg_ms > 10 else "🟢"
                label = f"{icon} **{avg_ms} ms** avg  |  {q['calls']} calls  |  {q['total_ms']} ms total"
                with st.expander(label):
                    st.code(q["query"], language="sql")
    except Exception as e:
        st.warning(
            "⚠️ Database is starting up — PostgreSQL briefly restarts after loading TPC-H data. "
            "This usually resolves in a few seconds."
        )
        if st.button("🔁 Retry Connection", key="retry_workload"):
            st.rerun()


# ─── Tab 2: Analyze & Approve (Human-in-the-Loop) ────────────────────────────
with tab2:
    st.subheader("AI Analysis + Human Approval Gate")
    st.info(
        "The agent analyzes your query workload and suggests indexes / materialized views. "
        "**Nothing is applied until you explicitly approve each change.**"
    )

    if st.button("🤖 Run Analysis", type="primary"):
        with st.spinner("Fetching workload and schema, then running AI analysis..."):
            try:
                workload = get_query_workload()
                if not workload:
                    st.warning(
                        "No workload data found. Run some queries first, then come back."
                    )
                    st.stop()

                schema = get_schema_summary()
                indexes = get_existing_indexes()
                index_str = (
                    "\n".join(f"  - {r[0]}: {r[2]}" for r in indexes)
                    if indexes
                    else "  None"
                )
                workload_str = "\n\n".join(
                    [
                        f"Query #{i+1} (avg: {q['avg_ms']}ms, calls: {q['calls']}, total: {q['total_ms']}ms):\n{q['query']}"
                        for i, q in enumerate(workload[:10])
                    ]
                )

                prompt = (
                    "Analyze this PostgreSQL database and produce an optimization plan.\n\n"
                    f"SCHEMA:\n{schema}\n\n"
                    f"EXISTING INDEXES:\n{index_str}\n\n"
                    f"TOP SLOW QUERIES (from pg_stat_statements):\n{workload_str}\n\n"
                    "Recommend the most impactful indexes and/or materialized views.\n"
                    "For each DDL recommendation provide exact executable SQL.\n"
                    "Also show how the affected queries should be rewritten to use these structures."
                )

                response = workload_optimizer_agent.run(prompt)
                plan = response.content
                st.session_state["plan"] = plan
                st.session_state["approvals"] = {
                    i: None for i in range(len(plan.ddl_recommendations))
                }
                st.session_state.pop("exec_results", None)

            except Exception as e:
                st.error(f"Analysis failed: {e}")

    if "plan" in st.session_state:
        plan = st.session_state["plan"]
        st.divider()
        st.markdown(f"**Summary:** {plan.summary}")
        st.divider()

        if plan.ddl_recommendations:
            st.subheader(f"DDL Recommendations ({len(plan.ddl_recommendations)} found)")

            for i, rec in enumerate(plan.ddl_recommendations):
                status = st.session_state["approvals"].get(i)
                badge = "📇" if rec.type == "index" else "🗂️"

                with st.container(border=True):
                    left, right_approve, right_reject = st.columns([5, 1, 1])

                    with left:
                        st.markdown(f"{badge} **{rec.type.upper()}** — `{rec.name}`")
                        st.caption(f"💡 {rec.reason}")
                        st.code(rec.ddl, language="sql")

                    with right_approve:
                        st.write("")
                        st.write("")
                        if st.button(
                            "✅ Approve",
                            key=f"approve_{i}",
                            use_container_width=True,
                            type="primary",
                        ):
                            st.session_state["approvals"][i] = "approved"
                            st.rerun()

                    with right_reject:
                        st.write("")
                        st.write("")
                        if st.button(
                            "❌ Reject", key=f"reject_{i}", use_container_width=True
                        ):
                            st.session_state["approvals"][i] = "rejected"
                            st.rerun()

                    if status == "approved":
                        st.success("✅ Approved — queued for execution")
                    elif status == "rejected":
                        st.error("❌ Rejected — will be skipped")
                    else:
                        st.caption("⏳ Awaiting your decision")

            approved_indices = [
                i for i, v in st.session_state["approvals"].items() if v == "approved"
            ]
            if approved_indices:
                st.divider()
                if st.button(
                    f"⚡ Execute {len(approved_indices)} Approved Change(s)",
                    type="primary",
                ):
                    results = []
                    for i in approved_indices:
                        rec = plan.ddl_recommendations[i]
                        try:
                            execute_ddl(rec.ddl)
                            results.append((rec.name, True, None))
                        except Exception as e:
                            results.append((rec.name, False, str(e)))
                    st.session_state["exec_results"] = results

        if "exec_results" in st.session_state:
            st.divider()
            st.subheader("Execution Results")
            for name, success, err in st.session_state["exec_results"]:
                if success:
                    st.success(f"✅ `{name}` created successfully")
                else:
                    st.error(f"❌ `{name}` — {err}")

        if plan.query_rewrites:
            st.divider()
            st.subheader("Suggested Query Rewrites")
            st.caption(
                "How your slow queries can be rewritten to benefit from the new indexes/views"
            )
            for rw in plan.query_rewrites:
                with st.expander("View rewrite"):
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.caption("🐌 Original Query")
                        st.code(rw.original_query, language="sql")
                    with col_b:
                        st.caption("🚀 Rewritten Query")
                        st.code(rw.rewritten_query, language="sql")
                    st.markdown(f"**Why it's faster:** {rw.explanation}")


# ─── Tab 3: Query Optimizer (with Benchmark) ──────────────────────────────────
with tab3:
    st.subheader("SQL Query Optimizer")
    st.caption(
        "Analyze, optimize, and benchmark individual SQL queries with AI-powered insights"
    )

    task = st.text_area(
        "📝 What are you trying to achieve?",
        placeholder="Example: Find all orders shipped in Q1 1995 with high revenue",
        height=80,
        key="optimizer_task",
    )

    unoptimized_sql = st.text_area(
        "💻 Your SQL Query",
        placeholder="SELECT * FROM orders WHERE ...",
        height=180,
        key="optimizer_sql",
    )

    btn_col1, btn_col2, btn_col3 = st.columns(3)
    with btn_col1:
        optimize_btn = st.button(
            "🚀 Analyze & Optimize", type="primary", use_container_width=True
        )
    with btn_col2:
        execute_btn = st.button(
            "▶️ Execute Optimized", type="secondary", use_container_width=True
        )
    with btn_col3:
        benchmark_btn = st.button(
            "📊 Benchmark",
            use_container_width=True,
            help="Compare original vs optimized with EXPLAIN ANALYZE",
        )

    st.markdown("---")

    if optimize_btn:
        if task and unoptimized_sql:
            st.session_state.pop("execution_result", None)
            st.session_state.pop("benchmark_report", None)
            st.session_state.pop("benchmark_data", None)
            st.session_state["original_sql"] = unoptimized_sql  # save for benchmark
            with st.spinner("🔍 Analyzing schema and optimizing query..."):
                ctx = gather_query_context(unoptimized_sql)
                prompt = f"""
Task: {task}

Unoptimized SQL Query:
```sql
{unoptimized_sql}
```

=== LIVE DATABASE CONTEXT (already fetched — do NOT re-run these) ===
{ctx}
=== END CONTEXT ===

Using the context above as ground truth:

### Findings
List every performance issue with direct evidence from the EXPLAIN output above
(e.g. "Seq Scan on lineitem — no index on l_shipdate confirmed by pg_indexes").

### Missing Indexes
For every column used in WHERE / JOIN / ORDER BY that does NOT appear in pg_indexes above,
write the exact CREATE INDEX statement. If no indexes are missing, say "None".

### Optimized Query
```sql
<CREATE INDEX statements first, then the SELECT>
```

### Explanation
For each change, state the before/after plan impact.
"""
                response = sql_optimizer_agent.run(prompt)
                st.session_state["optimization_result"] = response.content
                optimized = extract_optimized_sql(response.content)
                st.session_state["optimized_query"] = (
                    optimized if optimized else unoptimized_sql
                )
                # Sync the editable text_area widget — without this the widget's
                # key-based state takes precedence over the value= arg and
                # immediately overwrites optimized_query with the stale/empty value.
                st.session_state["optimized_display"] = st.session_state[
                    "optimized_query"
                ]
        else:
            st.warning("⚠️ Please provide both a task description and SQL query")

    if execute_btn:
        if st.session_state.get("optimized_query"):
            with st.spinner("⚡ Executing optimized query..."):
                cols, rows, err = execute_sql_directly(
                    st.session_state["optimized_query"]
                )
                if err:
                    st.session_state["execution_result"] = ("error", err)
                elif not rows:
                    st.session_state["execution_result"] = ("empty", cols)
                else:
                    st.session_state["execution_result"] = ("data", cols, rows)
        else:
            st.warning("⚠️ Please optimize the query first")

    if benchmark_btn:
        original_for_bench = st.session_state.get("original_sql") or unoptimized_sql
        if original_for_bench and st.session_state.get("optimized_query"):
            with st.spinner("Running benchmark (2 warmup + 5 measured runs)..."):
                try:
                    comparison = compare_queries(
                        db_host,
                        original_for_bench,
                        st.session_state["optimized_query"],
                        runs=5,
                        warmup=2,
                    )
                    st.session_state["benchmark_report"] = format_benchmark_report(
                        comparison
                    )
                    st.session_state["benchmark_data"] = comparison
                except Exception as e:
                    st.error(f"Benchmark failed: {e}")
        elif not original_for_bench:
            st.warning("Please paste your original SQL query and optimize first")
        else:
            st.warning("Please optimize the query first, then benchmark")

    if "execution_result" in st.session_state:
        st.divider()
        st.subheader("✅ Query Results")
        with st.expander("📋 View Executed SQL", expanded=False):
            st.code(st.session_state.get("optimized_query", ""), language="sql")
        result = st.session_state["execution_result"]
        if result[0] == "error":
            st.error(f"Execution failed: {result[1]}")
        elif result[0] == "empty":
            st.info("Query returned no rows.")
        else:
            _, cols, rows = result
            df = pd.DataFrame(rows, columns=cols)
            st.dataframe(df, use_container_width=True)
            st.caption(f"{len(rows)} row(s) returned")

    if "optimization_result" in st.session_state:
        st.divider()
        st.subheader("📊 Optimization Report")
        res_tab1, res_tab2, res_tab3 = st.tabs(
            ["Analysis & Recommendations", "Optimized SQL", "Benchmark Results"]
        )

        with res_tab1:
            st.markdown(st.session_state["optimization_result"])

        with res_tab2:
            optimized_query_display = st.text_area(
                "Optimized SQL Query",
                value=st.session_state.get("optimized_query", ""),
                height=250,
                key="optimized_display",
                help="Review and edit the optimized query before execution",
            )
            st.session_state["optimized_query"] = optimized_query_display
            st.info("💡 Click '▶️ Execute Optimized' above to run this SQL")

        with res_tab3:
            if "benchmark_report" in st.session_state:
                st.markdown(st.session_state["benchmark_report"])
                data = st.session_state.get("benchmark_data", {})
                orig = data.get("original", {})
                opt = data.get("optimized", {})
                if "error" not in orig and "error" not in opt:
                    st.divider()
                    st.subheader("Timing Breakdown")
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.metric(
                            "Original Execution Time",
                            f"{orig['execution_time_ms']:.3f} ms",
                        )
                        st.metric("Original Total Cost", f"{orig['total_cost']:.2f}")
                    with col_b:
                        exec_delta = (
                            orig["execution_time_ms"] - opt["execution_time_ms"]
                        )
                        cost_delta = orig["total_cost"] - opt["total_cost"]
                        st.metric(
                            "Optimized Execution Time",
                            f"{opt['execution_time_ms']:.3f} ms",
                            delta=(
                                f"-{exec_delta:.3f} ms"
                                if exec_delta > 0
                                else f"+{abs(exec_delta):.3f} ms"
                            ),
                            delta_color="normal" if exec_delta > 0 else "inverse",
                        )
                        st.metric(
                            "Optimized Total Cost",
                            f"{opt['total_cost']:.2f}",
                            delta=(
                                f"-{cost_delta:.2f}"
                                if cost_delta > 0
                                else f"+{abs(cost_delta):.2f}"
                            ),
                            delta_color="normal" if cost_delta > 0 else "inverse",
                        )
            else:
                st.info("Click '📊 Benchmark' above to run a performance comparison")


# ─── Tab 4: Query Rewriter ────────────────────────────────────────────────────
with tab4:
    st.subheader("Rewrite a Query")
    st.caption(
        "Paste any query and the agent rewrites it to leverage available indexes and materialized views"
    )

    query_input = st.text_area(
        "SQL Query:",
        height=150,
        placeholder="SELECT * FROM orders WHERE user_id = 123 ORDER BY created_at DESC;",
    )
    context_input = st.text_area(
        "Additional context (optional):",
        height=80,
        placeholder="e.g. We just created an index on orders(user_id). Please rewrite to benefit from it.",
    )

    if st.button("✍️ Rewrite Query", type="primary"):
        if query_input.strip():
            with st.spinner("Rewriting..."):
                try:
                    schema = get_schema_summary()
                    indexes = get_existing_indexes()
                    index_str = (
                        "\n".join(f"  - {r[0]}: {r[2]}" for r in indexes)
                        if indexes
                        else "  None"
                    )
                    context_section = (
                        f"CONTEXT: {context_input}\n\n" if context_input.strip() else ""
                    )
                    prompt = (
                        f"Rewrite this SQL query for better performance in PostgreSQL.\n\n"
                        f"SCHEMA:\n{schema}\n\n"
                        f"AVAILABLE INDEXES:\n{index_str}\n\n"
                        f"{context_section}"
                        f"QUERY TO REWRITE:\n{query_input}"
                    )
                    response = rewriter_agent.run(prompt)
                    st.session_state["rewrite_result"] = response.content
                except Exception as e:
                    st.error(f"Rewrite failed: {e}")
        else:
            st.warning("Please enter a SQL query first")

    if "rewrite_result" in st.session_state:
        st.divider()
        st.markdown(st.session_state["rewrite_result"])


# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Database Info")
    st.info(
        f"""
**Database:** unoptimized_db  
**Type:** PostgreSQL 15  
**Status:** Connected  
**Host:** {db_host}
    """
    )

    st.divider()

    st.subheader("Run Manual Query")
    manual_query = st.text_area(
        "SQL Query",
        height=120,
        key="manual_query_input",
        placeholder="SELECT * FROM lineitem LIMIT 5;",
    )

    if st.button("Execute", use_container_width=True, key="manual_exec_btn"):
        if manual_query.strip():
            with st.spinner("Executing..."):
                cols, rows, err = execute_sql_directly(manual_query.strip())
                if err:
                    st.session_state["manual_result"] = ("error", err)
                elif not rows:
                    st.session_state["manual_result"] = ("empty", cols)
                else:
                    st.session_state["manual_result"] = ("data", cols, rows)
        else:
            st.warning("Please enter a query")

    if "manual_result" in st.session_state:
        st.subheader("Result")
        result = st.session_state["manual_result"]
        if result[0] == "error":
            st.error(result[1])
        elif result[0] == "empty":
            st.info("Query returned no rows.")
        else:
            _, cols, rows = result
            st.dataframe(pd.DataFrame(rows, columns=cols), use_container_width=True)
