import streamlit as st
from dotenv import load_dotenv
from workload import get_query_workload, get_schema_summary, get_existing_indexes, execute_ddl, reset_stats
from agents import optimizer_agent, rewriter_agent, OptimizationPlan

load_dotenv()

st.set_page_config(page_title="DB Optimizer Agent", page_icon="🧠", layout="wide")

st.title("🧠 AI Database Optimizer")
st.caption("Analyzes query workload → suggests indexes & materialized views → waits for your approval before applying anything")
st.divider()

tab1, tab2, tab3 = st.tabs(["📊 Workload Monitor", "🤖 Analyze & Approve", "✍️ Query Rewriter"])


# ─── Tab 1: Workload Monitor ──────────────────────────────────────────
with tab1:
    st.subheader("Query Workload from pg_stat_statements")
    st.caption("Tracks all queries run against the database since the last stats reset")

    col1, col2, col3 = st.columns([1, 1, 5])
    with col1:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
    with col2:
        if st.button("🗑️ Reset Stats", use_container_width=True, help="Clears pg_stat_statements history"):
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
            st.info("No workload data yet. Run some queries against your database, then refresh.")
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
        st.error(f"Error fetching workload: {e}")


# ─── Tab 2: Analyze & Approve (Human-in-the-Loop) ──────────────────────────────
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
                    st.warning("No workload data found. Run some queries first, then come back.")
                    st.stop()

                schema = get_schema_summary()
                indexes = get_existing_indexes()
                index_str = (
                    "\n".join(f"  - {r[0]}: {r[2]}" for r in indexes)
                    if indexes else "  None"
                )
                workload_str = "\n\n".join([
                    f"Query #{i+1} (avg: {q['avg_ms']}ms, calls: {q['calls']}, total: {q['total_ms']}ms):\n{q['query']}"
                    for i, q in enumerate(workload[:10])
                ])

                prompt = (
                    "Analyze this PostgreSQL database and produce an optimization plan.\n\n"
                    f"SCHEMA:\n{schema}\n\n"
                    f"EXISTING INDEXES:\n{index_str}\n\n"
                    f"TOP SLOW QUERIES (from pg_stat_statements):\n{workload_str}\n\n"
                    "Recommend the most impactful indexes and/or materialized views.\n"
                    "For each DDL recommendation provide exact executable SQL.\n"
                    "Also show how the affected queries should be rewritten to use these structures."
                )

                response = optimizer_agent.run(prompt)
                plan = response.content
                st.session_state["plan"] = plan
                st.session_state["approvals"] = {i: None for i in range(len(plan.ddl_recommendations))}
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
                        if st.button("✅ Approve", key=f"approve_{i}", use_container_width=True, type="primary"):
                            st.session_state["approvals"][i] = "approved"
                            st.rerun()

                    with right_reject:
                        st.write("")
                        st.write("")
                        if st.button("❌ Reject", key=f"reject_{i}", use_container_width=True):
                            st.session_state["approvals"][i] = "rejected"
                            st.rerun()

                    if status == "approved":
                        st.success("✅ Approved — queued for execution")
                    elif status == "rejected":
                        st.error("❌ Rejected — will be skipped")
                    else:
                        st.caption("⏳ Awaiting your decision")

            approved_indices = [i for i, v in st.session_state["approvals"].items() if v == "approved"]
            if approved_indices:
                st.divider()
                if st.button(f"⚡ Execute {len(approved_indices)} Approved Change(s)", type="primary"):
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
            st.caption("How your slow queries can be rewritten to benefit from the new indexes/views")
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


# ─── Tab 3: Query Rewriter ────────────────────────────────────────────────
with tab3:
    st.subheader("Rewrite a Query")
    st.caption("Paste any query and the agent rewrites it to leverage available indexes and materialized views")

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
                        if indexes else "  None"
                    )
                    context_section = f"CONTEXT: {context_input}\n\n" if context_input.strip() else ""
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
