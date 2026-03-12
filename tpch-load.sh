#!/bin/bash
# =============================================================================
# TPC-H database initializer
# Uses the official dbgen tool compiled inside the postgres container image.
#
# Scale factor guide (set TPCH_SCALE env var in docker-compose to override):
#   SF 0.1  =>  ~600K lineitem rows  (default, fast to load, good for CI/dev)
#   SF 1    =>  ~6M   lineitem rows  (standard TPC-H SF1)
#   SF 10   =>  ~60M  lineitem rows  (serious DBA benchmarking)
# =============================================================================
set -e

DBGEN=/tpch-kit/dbgen
SCALE=${TPCH_SCALE:-0.1}
WORKDIR=/tmp/tpch-data
PSQL="psql -v ON_ERROR_STOP=1 --username=$POSTGRES_USER --dbname=$POSTGRES_DB"

echo "[tpch] Generating TPC-H data at scale factor $SCALE ..."
mkdir -p "$WORKDIR"
# Run from dbgen dir so dists.dss is found; DSS_PATH redirects .tbl output to a writable location
cd "$DBGEN"
DSS_PATH="$WORKDIR" ./dbgen -vf -s "$SCALE"

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
echo "[tpch] Creating TPC-H schema ..."
$PSQL <<'SQL'

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE region (
    r_regionkey INTEGER       PRIMARY KEY,
    r_name      CHAR(25)      NOT NULL,
    r_comment   VARCHAR(152)
);

CREATE TABLE nation (
    n_nationkey INTEGER       PRIMARY KEY,
    n_name      CHAR(25)      NOT NULL,
    n_regionkey INTEGER       NOT NULL REFERENCES region(r_regionkey),
    n_comment   VARCHAR(152)
);

CREATE TABLE supplier (
    s_suppkey   INTEGER        PRIMARY KEY,
    s_name      CHAR(25)       NOT NULL,
    s_address   VARCHAR(40)    NOT NULL,
    s_nationkey INTEGER        NOT NULL REFERENCES nation(n_nationkey),
    s_phone     CHAR(15)       NOT NULL,
    s_acctbal   DECIMAL(15,2)  NOT NULL,
    s_comment   VARCHAR(101)   NOT NULL
);

CREATE TABLE customer (
    c_custkey    INTEGER        PRIMARY KEY,
    c_name       VARCHAR(25)    NOT NULL,
    c_address    VARCHAR(40)    NOT NULL,
    c_nationkey  INTEGER        NOT NULL REFERENCES nation(n_nationkey),
    c_phone      CHAR(15)       NOT NULL,
    c_acctbal    DECIMAL(15,2)  NOT NULL,
    c_mktsegment CHAR(10)       NOT NULL,
    c_comment    VARCHAR(117)   NOT NULL
);

CREATE TABLE part (
    p_partkey     INTEGER        PRIMARY KEY,
    p_name        VARCHAR(55)    NOT NULL,
    p_mfgr        CHAR(25)       NOT NULL,
    p_brand       CHAR(10)       NOT NULL,
    p_type        VARCHAR(25)    NOT NULL,
    p_size        INTEGER        NOT NULL,
    p_container   CHAR(10)       NOT NULL,
    p_retailprice DECIMAL(15,2)  NOT NULL,
    p_comment     VARCHAR(23)    NOT NULL
);

CREATE TABLE partsupp (
    ps_partkey    INTEGER        NOT NULL REFERENCES part(p_partkey),
    ps_suppkey    INTEGER        NOT NULL REFERENCES supplier(s_suppkey),
    ps_availqty   INTEGER        NOT NULL,
    ps_supplycost DECIMAL(15,2)  NOT NULL,
    ps_comment    VARCHAR(199)   NOT NULL,
    PRIMARY KEY (ps_partkey, ps_suppkey)
);

CREATE TABLE orders (
    o_orderkey      INTEGER        PRIMARY KEY,
    o_custkey       INTEGER        NOT NULL REFERENCES customer(c_custkey),
    o_orderstatus   CHAR(1)        NOT NULL,
    o_totalprice    DECIMAL(15,2)  NOT NULL,
    o_orderdate     DATE           NOT NULL,
    o_orderpriority CHAR(15)       NOT NULL,
    o_clerk         CHAR(15)       NOT NULL,
    o_shippriority  INTEGER        NOT NULL,
    o_comment       VARCHAR(79)    NOT NULL
);

CREATE TABLE lineitem (
    l_orderkey      INTEGER        NOT NULL REFERENCES orders(o_orderkey),
    l_partkey       INTEGER        NOT NULL,
    l_suppkey       INTEGER        NOT NULL,
    l_linenumber    INTEGER        NOT NULL,
    l_quantity      DECIMAL(15,2)  NOT NULL,
    l_extendedprice DECIMAL(15,2)  NOT NULL,
    l_discount      DECIMAL(15,2)  NOT NULL,
    l_tax           DECIMAL(15,2)  NOT NULL,
    l_returnflag    CHAR(1)        NOT NULL,
    l_linestatus    CHAR(1)        NOT NULL,
    l_shipdate      DATE           NOT NULL,
    l_commitdate    DATE           NOT NULL,
    l_receiptdate   DATE           NOT NULL,
    l_shipinstruct  CHAR(25)       NOT NULL,
    l_shipmode      CHAR(10)       NOT NULL,
    l_comment       VARCHAR(44)    NOT NULL,
    PRIMARY KEY (l_orderkey, l_linenumber),
    FOREIGN KEY (l_partkey, l_suppkey) REFERENCES partsupp(ps_partkey, ps_suppkey)
);

SQL

# --------------------------------------------------------------------------
# Load data
# dbgen appends a trailing '|' to every line — strip it with sed before COPY
# Load order must respect FK dependencies
# --------------------------------------------------------------------------
echo "[tpch] Loading data ..."
for table in region nation supplier customer part partsupp orders lineitem; do
    echo "[tpch]   COPY $table ..."
    sed 's/|$//' "$WORKDIR/${table}.tbl" | \
        $PSQL -c "\COPY $table FROM STDIN WITH (FORMAT csv, DELIMITER '|')"
done

# --------------------------------------------------------------------------
# Indexes  (TPC-H recommended access paths)
# --------------------------------------------------------------------------
echo "[tpch] Creating indexes ..."
$PSQL <<'SQL'

CREATE INDEX idx_lineitem_shipdate    ON lineitem (l_shipdate);
CREATE INDEX idx_lineitem_orderkey    ON lineitem (l_orderkey);
CREATE INDEX idx_lineitem_partkey     ON lineitem (l_partkey);
CREATE INDEX idx_lineitem_suppkey     ON lineitem (l_suppkey);
CREATE INDEX idx_lineitem_flags       ON lineitem (l_returnflag, l_linestatus);
CREATE INDEX idx_orders_custkey       ON orders   (o_custkey);
CREATE INDEX idx_orders_orderdate     ON orders   (o_orderdate);
CREATE INDEX idx_orders_orderstatus   ON orders   (o_orderstatus);
CREATE INDEX idx_customer_nationkey   ON customer (c_nationkey);
CREATE INDEX idx_customer_mktsegment  ON customer (c_mktsegment);
CREATE INDEX idx_supplier_nationkey   ON supplier (s_nationkey);
CREATE INDEX idx_partsupp_suppkey     ON partsupp (ps_suppkey);
CREATE INDEX idx_part_type            ON part     (p_type);
CREATE INDEX idx_part_mfgr            ON part     (p_mfgr);

-- Update planner stats after bulk load
ANALYZE;

SQL

echo "[tpch] Done — TPC-H SF ${SCALE} loaded into $POSTGRES_DB successfully."
