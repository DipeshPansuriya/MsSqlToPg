-- MsSqlToPg v2.16.0 -- export the column types for rule A054.
--
-- READ-ONLY. Run it against the target PostgreSQL database in DBeaver, export the
-- single column as plain text (no header needed), and save the result as
-- column_types.txt next to MsSqlToPg.exe (or next to app.py).
-- (No database access? python -m tools.schema_from_ddl <pg_dump --schema-only file>.)
SELECT lower(table_name) || '.' || lower(column_name) || chr(9) ||
       CASE WHEN data_type = 'USER-DEFINED' THEN udt_name ELSE data_type END AS column_type
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
