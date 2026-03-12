import psycopg
import os
import time
from dotenv import load_dotenv

load_dotenv()

_db_host = os.getenv("DB_HOST", "postgres")
AI_CONN = (
    f"host={_db_host} port=5432 dbname=unoptimized_db user=ai_user password=secret"
)
PG_CONN = f"host={_db_host} port=5432 dbname=unoptimized_db user=postgres password={os.getenv('POSTGRES_PASSWORD', 'postgres123')}"


def _connect(
    conn_str: str, autocommit: bool = False, retries: int = 15, delay: float = 3.0
):
    """Connect with retries to handle postgres startup/restart windows."""
    last_err = None
    for attempt in range(retries):
        try:
            return psycopg.connect(conn_str, autocommit=autocommit)
        except psycopg.OperationalError as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise last_err


NOISE_FILTERS = [
    "%pg_stat%",
    "%pg_catalog%",
    "%information_schema%",
    "%pg_show_all%",
    "%set_config%",
    "%pg_extension%",
    "%pg_indexes%",
    "%pg_class%",
    "%pg_namespace%",
]


def get_query_workload():
    """Fetch top slow queries from pg_stat_statements."""
    with _connect(AI_CONN) as conn:
        with conn.cursor() as cur:
            where_clauses = " AND ".join(
                [f"query NOT LIKE '{f}'" for f in NOISE_FILTERS]
            )
            cur.execute(
                f"""
                SELECT
                    query,
                    calls,
                    round(mean_exec_time::numeric, 2) AS avg_ms,
                    round(total_exec_time::numeric, 2) AS total_ms,
                    rows
                FROM pg_stat_statements
                WHERE {where_clauses}
                AND query NOT ILIKE 'SET %'
                AND query NOT ILIKE 'GRANT %'
                AND query NOT ILIKE 'SHOW %'
                AND query NOT ILIKE 'BEGIN%'
                AND query NOT ILIKE 'COMMIT%'
                AND query NOT ILIKE 'ROLLBACK%'
                AND calls > 0
                ORDER BY mean_exec_time DESC
                LIMIT 20
            """
            )
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                d["avg_ms"] = float(d["avg_ms"])
                d["total_ms"] = float(d["total_ms"])
                rows.append(d)
            return rows


def get_schema_summary():
    """Return schema info as a formatted string for agent context."""
    with _connect(AI_CONN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name NOT IN ('pg_stat_statements', 'pg_stat_statements_info')
                ORDER BY table_name, ordinal_position
            """
            )
            rows = cur.fetchall()

    schema: dict = {}
    for table, col, dtype in rows:
        if table not in schema:
            schema[table] = []
        schema[table].append(f"{col} ({dtype})")

    return "\n".join([f"Table '{t}': {', '.join(cols)}" for t, cols in schema.items()])


def get_existing_indexes():
    """Return list of (tablename, indexname, indexdef) for non-PK indexes."""
    with _connect(AI_CONN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                AND indexname NOT LIKE '%_pkey'
                ORDER BY tablename, indexname
            """
            )
            return cur.fetchall()


def execute_ddl(sql: str):
    """Execute a DDL statement using ai_user (must own the tables)."""
    with _connect(AI_CONN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def reset_stats():
    """Reset pg_stat_statements — requires postgres superuser."""
    with _connect(PG_CONN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_stat_statements_reset()")
