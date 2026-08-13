#!/bin/bash
# Phase D0 — create the production + test databases with SEPARATE least-privilege users.
# Runs once, on first PostgreSQL init. Credentials come from the container environment (the env
# file), never hardcoded here. UTF-8 + UTC are set at the cluster level.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname postgres <<SQL
    -- application (production) role + database
    CREATE ROLE "${ATP_APP_USER}" LOGIN PASSWORD '${ATP_APP_PASSWORD}';
    CREATE DATABASE "${ATP_PROD_DB}" OWNER "${ATP_APP_USER}" ENCODING 'UTF8'
        LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
    REVOKE ALL ON DATABASE "${ATP_PROD_DB}" FROM PUBLIC;
    GRANT ALL PRIVILEGES ON DATABASE "${ATP_PROD_DB}" TO "${ATP_APP_USER}";

    -- test role + database (isolated credentials; used by the Phase B.5 integration suite)
    CREATE ROLE "${ATP_TEST_USER}" LOGIN PASSWORD '${ATP_TEST_PASSWORD}';
    CREATE DATABASE "${ATP_TEST_DB}" OWNER "${ATP_TEST_USER}" ENCODING 'UTF8'
        LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
    REVOKE ALL ON DATABASE "${ATP_TEST_DB}" FROM PUBLIC;
    GRANT ALL PRIVILEGES ON DATABASE "${ATP_TEST_DB}" TO "${ATP_TEST_USER}";
SQL

echo "created databases ${ATP_PROD_DB} (owner ${ATP_APP_USER}) and ${ATP_TEST_DB} (owner ${ATP_TEST_USER})"
