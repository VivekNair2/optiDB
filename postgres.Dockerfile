FROM postgres:15

# Install build tools needed to compile dbgen
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git gcc make libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone and compile TPC-H dbgen
RUN git clone --depth 1 https://github.com/gregrahn/tpch-kit.git /tpch-kit \
    && cd /tpch-kit/dbgen \
    && make MACHINE=LINUX DATABASE=POSTGRESQL

# Copy init script
COPY tpch-load.sh /docker-entrypoint-initdb.d/01-tpch-load.sh

# Strip Windows CRLF line endings and make executable
RUN sed -i 's/\r$//' /docker-entrypoint-initdb.d/01-tpch-load.sh \
    && chmod +x /docker-entrypoint-initdb.d/01-tpch-load.sh

# Enable pg_stat_statements at server startup
CMD ["postgres", "-c", "shared_preload_libraries=pg_stat_statements", "-c", "pg_stat_statements.track=all"]
