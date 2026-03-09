import streamlit as st
from agno.agent import Agent
from agno.tools.sql import SQLTools
from agno.models.openai import OpenAIChat
from dotenv import load_dotenv
import os
import re
import time
import psycopg
from datetime import datetime
from parser import parse_logs

load_dotenv()

os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY")
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")

# Use postgres hostname for Docker, localhost for local
db_host = os.getenv("DB_HOST", "postgres")
db_url = f"postgresql+psycopg://ai_user:secret@{db_host}:5432/unoptimized_db"


# Helper function to get live query workload
def get_live_queries():
    """Fetch currently running queries from the database."""
    try:
        conn_info = f"host={db_host} port=5432 dbname=unoptimized_db user=ai_user password=secret"
        with psycopg.connect(conn_info) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        pid,
                        usename,
                        state,
                        EXTRACT(EPOCH FROM (now() - query_start)) AS running_time,
                        query_start,
                        LEFT(query, 150) AS query
                    FROM pg_stat_activity
                    WHERE state != 'idle'
                    ORDER BY running_time DESC
                    LIMIT 10;
                """
                )
                results = cur.fetchall()
                return results if results else []
    except Exception as e:
        # Return error tuple for debugging
        return {"error": str(e)}


# Helper function to extract optimized SQL from agent output
def extract_optimized_sql(text: str) -> str:
    """Extract the optimized SQL query from the agent's output text."""
    # Try multiple strategies to find the optimized SQL

    # Strategy 1: Look for fenced code blocks with sql
    fence_pattern = r"```sql\s*(.*?)\s*```"
    matches = re.findall(fence_pattern, text, re.DOTALL | re.IGNORECASE)
    if matches:
        # Return the last SQL block (usually the optimized one)
        return matches[-1].strip()

    # Strategy 2: Look for "Optimized Query:" header
    opt_patterns = [
        r"Optimized Query:\s*(SELECT.*?)(?:\n\n|Explanation|Recommended)",
        r"Optimized SQL:\s*(SELECT.*?)(?:\n\n|Explanation|Recommended)",
        r"(?:Optimized Query|Optimized SQL):\s*(SELECT.*?)(?:\n\n|\Z)",
    ]

    for pattern in opt_patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()

    # Strategy 3: Find any SELECT statement that looks complete
    select_pattern = r"(SELECT\s+.+?FROM.+?;)"
    matches = re.findall(select_pattern, text, re.DOTALL | re.IGNORECASE)
    if matches:
        # Return the longest one (usually more complete)
        return max(matches, key=len).strip()

    # Fallback: return empty string
    return ""


# SQL Optimizer Agent
optimizer_agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[SQLTools(db_url=db_url)],
    instructions=[
        "You are a database optimization expert",
        "Use list_tables and describe_table to understand the actual database schema",
        "Analyze the provided unoptimized SQL query against the live schema",
        "Identify performance issues (missing indexes, inefficient joins, SELECT *, etc.)",
        "Provide an optimized version of the query",
        "Explain each optimization with detailed reasoning",
        "Suggest indexes, query rewrites, or schema improvements if needed",
    ],
    markdown=True,
)

# SQL Executor Agent
executor_agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[SQLTools(db_url=db_url)],
    instructions=[
        "You are a SQL execution assistant",
        "IMPORTANT: You MUST execute the SQL query using the run_sql_query tool",
        "DO NOT just describe what the query does - actually execute it and return the results",
        "After executing, format the results as a table or list",
        "Show the actual data rows returned by the query",
        "If the query returns no rows, say 'No results found'",
        "If query fails, show the error message",
    ],
    markdown=True,
)

# Streamlit UI
st.set_page_config(page_title="SQL Optimizer", page_icon="🔧", layout="wide")

st.title("🔧 SQL Query Optimizer")
st.write("Analyze, optimize, and execute your SQL queries with AI-powered insights")

st.divider()

# Input section
st.subheader("📝 Describe Your Goal")
task = st.text_area(
    "What are you trying to achieve?",
    placeholder="Example: Find all users who made purchases over $500 in the last month",
    height=100,
    help="Explain what data you're trying to retrieve",
)

st.subheader("💻 Your SQL Query")
unoptimized_sql = st.text_area(
    "Paste your SQL query",
    placeholder="SELECT * FROM users WHERE ...",
    height=180,
    help="Paste the SQL query you want to analyze and optimize",
)

st.divider()

# Action buttons
col1, col2 = st.columns(2)

with col1:
    optimize_btn = st.button(
        "🚀 Analyze & Optimize Query", type="primary", use_container_width=True
    )

with col2:
    execute_btn = st.button(
        "▶️ Execute Optimized Query", type="secondary", use_container_width=True
    )

st.markdown("---")

# Handle Optimization
if optimize_btn:
    if task and unoptimized_sql:
        # Clear previous execution results when optimizing
        if "execution_result" in st.session_state:
            del st.session_state["execution_result"]

        with st.spinner("🔍 Analyzing schema and optimizing query..."):
            prompt = f"""
            Task: {task}
            
            Unoptimized SQL Query:
            ```sql
            {unoptimized_sql}
            ```
            
            Please:
            1. Inspect the actual database schema using list_tables and describe_table
            2. Analyze the query against the real schema
            3. if the table name and columns are wrong then write ur own sql query based on correct info
            3. Identify all performance issues
            4. Provide an optimized version of the query
            5. Explain each optimization with reasoning
            6. Suggest any additional indexes or schema changes
            7. Dont include any sql for index creation and stuff
            """

            response = optimizer_agent.run(prompt)
            st.session_state["optimization_result"] = response.content

            # Extract the optimized SQL from the agent's response
            optimized = extract_optimized_sql(response.content)
            if optimized:
                st.session_state["optimized_query"] = optimized
            else:
                st.session_state["optimized_query"] = unoptimized_sql
    else:
        st.warning("⚠️ Please provide both a task description and SQL query")

# Handle Execution
if execute_btn:
    if "optimized_query" in st.session_state and st.session_state["optimized_query"]:
        with st.spinner("⚡ Executing optimized query..."):
            execute_prompt = f"""
            Use the run_sql_query tool to execute this SQL query and return the actual data results:
            
            ```sql
            {st.session_state['optimized_query']}
            ```
            
            IMPORTANT: 
            - You MUST call run_sql_query tool to execute this query
            - Return the actual data rows from the database
            - Format the results as a markdown table showing all columns and rows
            - Do NOT just describe what the query does
            """

            response = executor_agent.run(execute_prompt)
            st.session_state["execution_result"] = response.content
            # Mark that we just executed
            st.session_state["just_executed"] = True
    else:
        st.warning("⚠️ Please optimize the query first")

# Display results
if "execution_result" in st.session_state:
    st.divider()
    st.subheader("✅ Query Results")

    with st.expander("📋 View Executed SQL", expanded=False):
        st.code(st.session_state.get("optimized_query", ""), language="sql")

    st.markdown(st.session_state["execution_result"])

if "optimization_result" in st.session_state:
    st.divider()
    st.subheader("📊 Optimization Report")

    tab1, tab2 = st.tabs(["Analysis & Recommendations", "Optimized SQL"])

    with tab1:
        st.markdown(st.session_state["optimization_result"])

    with tab2:
        optimized_query_display = st.text_area(
            "Optimized SQL Query",
            value=st.session_state.get("optimized_query", ""),
            height=250,
            help="Review and edit the optimized query before execution",
        )
        st.session_state["optimized_query"] = optimized_query_display

        st.info("💡 Click the 'Execute Optimized Query' button above to run this SQL")

# Sidebar
with st.sidebar:
    st.header("Settings")

    st.subheader("Database Info")
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
        placeholder="SELECT * FROM users LIMIT 10;",
    )

    if st.button("Execute", use_container_width=True, key="manual_exec_btn"):
        if manual_query.strip():
            with st.spinner("Executing..."):
                try:
                    response = executor_agent.run(
                        f"Execute: ```sql\n{manual_query}\n```"
                    )
                    st.session_state["manual_result"] = response.content
                except Exception as e:
                    st.session_state["manual_result"] = f"Error: {str(e)}"
        else:
            st.warning("Please enter a query")

    if "manual_result" in st.session_state:
        st.subheader("Result")
        st.markdown(st.session_state["manual_result"])

    st.divider()

    st.subheader("How to Use")
    st.write(
        """
1. Describe what you need
2. Paste your SQL query
3. Click 'Analyze & Optimize'
4. Review the suggestions
5. Execute the optimized query
    """
    )

    st.divider()

    st.subheader("Features")
    st.write(
        """
- Live schema analysis
- Performance insights
- Index recommendations
- Query execution
- Error detection
    """
    )
