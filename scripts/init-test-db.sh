#!/bin/sh
# Creates the test database next to the real one, on FIRST init of the pgdata volume only
# (that's the contract of /docker-entrypoint-initdb.d — it never runs against existing data).
#
# The suite gives every test its own schema inside this database and truncates between tests,
# which is exactly why it must never point at $POSTGRES_DB.
set -e

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE DATABASE ${POSTGRES_DB}_test OWNER ${POSTGRES_USER};
SQL

echo "init: created ${POSTGRES_DB}_test"
