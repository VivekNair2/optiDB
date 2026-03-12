# optiDB — AI-Powered SQL Query Optimizer

optiDB is a Streamlit application that uses GPT-4o to analyze, optimize, and benchmark PostgreSQL queries against a real TPC-H dataset. It tracks your query workload via `pg_stat_statements`, recommends missing indexes, and produces side-by-side `EXPLAIN ANALYZE` benchmarks showing the before/after impact.

---

## Features

| Tab | What it does |
|---|---|
| **Workload Monitor** | Live view of slowest queries from `pg_stat_statements` |
| **Analyze & Approve** | AI suggests indexes/views from workload; you approve before anything runs |
| **Query Optimizer** | Paste any SQL → AI optimizes it → Execute & Benchmark |
| **Query Rewriter** | Rewrites queries for readability without changing semantics |

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop | 4.x+ | Must be running |
| Docker Compose | v2 (bundled with Docker Desktop) | `docker compose` not `docker-compose` |
| OpenAI API key | — | GPT-4o access required |

No Python installation needed — everything runs inside Docker.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/VivekNair2/optiDB.git
cd optidb
```

### 2. Set up environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your API key:

```env
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...        # optional, not used by default
```

### 3. Build and start

```bash
docker compose up -d --build
```

This will:
- Build the **postgres image** — compiles TPC-H `dbgen` from source (cached after first build)
- Build the **app image** — installs Python dependencies
- Start **PostgreSQL 15** and load ~600K rows of TPC-H SF 0.1 data
- Start the **Streamlit app** (waits for postgres to finish loading)

> **First run takes 3–5 minutes** — TPC-H data generation and loading happens inside the container on startup. The app container will not start until the database is fully ready.

### 4. Open the app

Once `docker compose up` returns and both containers are healthy:

```
http://localhost:8501
```

Check container status at any time:

```bash
docker ps
```

Both containers should show `healthy` or `Up`:

```
NAMES             STATUS
optidb_app        Up 2 minutes
optidb_postgres   Up 3 minutes (healthy)
```

---

## Project Structure

```
optidb/
├── app.py                  # Main Streamlit app (4 tabs)
├── agents.py               # Agno agent definitions + Pydantic output schemas
├── workload.py             # PostgreSQL workload/schema introspection utilities
├── benchmarker.py          # EXPLAIN ANALYZE benchmark runner
├── parser.py               # SQL parsing utilities
├── Dockerfile              # App container (python:3.11-slim + Streamlit)
├── postgres.Dockerfile     # Postgres container (compiles TPC-H dbgen)
├── tpch-load.sh            # Init script: generates + loads TPC-H data
├── docker-compose.yml      # Orchestrates both containers
├── requirements.txt        # Python dependencies
├── .env.example            # Template for environment variables
└── init.sql                # (unused in TPC-H mode)
```

---

## Database

The postgres container runs **PostgreSQL 15** with:

| Setting | Value |
|---|---|
| Host (in Docker) | `postgres` |
| Host (from host machine) | `localhost` |
| Port | `5432` |
| Database | `unoptimized_db` |
| User | `ai_user` |
| Password | `secret` |

**TPC-H tables loaded:** `region`, `nation`, `supplier`, `customer`, `part`, `partsupp`, `orders`, `lineitem`

**Scale factor:** SF 0.1 (~600K lineitem rows, 150K orders, 15K customers)

**Extensions:** `pg_stat_statements` (enabled automatically)

### Connect directly from your machine

```bash
psql -h localhost -U ai_user -d unoptimized_db
# password: secret
```

---

## Development Workflow

The app source directory is **bind-mounted** into the container (`.:/app`), so code changes are reflected immediately — no rebuild needed for Python file edits.

```bash
# After editing any .py file, just refresh the browser
# Streamlit's hot-reload picks it up automatically

# Rebuild only when Dockerfile or requirements.txt changes
docker compose up -d --build
```

### Useful commands

```bash
# View logs
docker logs optidb_app -f
docker logs optidb_postgres -f

# Restart just the app (e.g. after a crash)
docker compose restart app

# Open a postgres shell
docker exec -it optidb_postgres psql -U ai_user -d unoptimized_db

# Check TPC-H data loaded correctly
docker exec optidb_postgres psql -U ai_user -d unoptimized_db \
  -c "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"

# Reset pg_stat_statements query history
docker exec optidb_postgres psql -U ai_user -d unoptimized_db \
  -c "SELECT pg_stat_statements_reset();"

# Full teardown (WARNING: deletes all postgres data)
docker compose down -v
```

---

## Example Queries to Try

These queries hit unindexed columns and produce meaningful benchmark results:

**Late shipments by supplier** (hits `l_commitdate`, `l_receiptdate`, `l_suppkey` — all unindexed):
```sql
SELECT
    s.s_name,
    COUNT(l.l_orderkey)       AS late_shipments,
    SUM(l.l_extendedprice)    AS total_revenue
FROM supplier s
JOIN partsupp ps  ON s.s_suppkey  = ps.ps_suppkey
JOIN lineitem l   ON ps.ps_suppkey = l.l_suppkey
                 AND ps.ps_partkey  = l.l_partkey
JOIN orders o     ON l.l_orderkey  = o.o_orderkey
WHERE l.l_commitdate < l.l_receiptdate
  AND o.o_orderstatus = 'F'
  AND l.l_quantity > 20
GROUP BY s.s_name
ORDER BY late_shipments DESC
LIMIT 10;
```

**High-value customers in a segment** (hits `c_mktsegment`, `o_totalprice` — both unindexed):
```sql
SELECT c.c_name, SUM(l.l_extendedprice) AS total_spend
FROM customer c
JOIN orders o  ON c.c_custkey = o.o_custkey
JOIN lineitem l ON o.o_orderkey = l.l_orderkey
WHERE c.c_mktsegment = 'BUILDING'
  AND o.o_totalprice > 100000
GROUP BY c.c_name
ORDER BY total_spend DESC
LIMIT 20;
```

---

## Troubleshooting

**"Error fetching workload: connection refused"**
Postgres is still loading TPC-H data. Click the **🔁 Retry Connection** button or wait ~30 seconds and refresh.

**"relation customer does not exist"**
The volume was wiped (e.g. after `docker compose down -v`). Run `docker compose up -d --build` — TPC-H data will reload automatically.

**"TLS handshake timeout" during build**
Docker Hub rate limit or network issue. Run `docker compose up -d` (without `--build`) — if images are already cached locally they will be reused.

**App not reflecting code changes**
The volume mount means edits are live immediately. If Streamlit doesn't hot-reload, press `R` in the browser or run `docker compose restart app`.

**Port 5432 or 8501 already in use**
Stop conflicting services, or change the host port mapping in `docker-compose.yml` (e.g. `"5433:5432"`).
