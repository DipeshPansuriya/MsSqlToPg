-- MsSqlToPg v2.13.0 -- export the BOOLEAN column list for rule A024.
--
-- READ-ONLY. Run it against the ACS PostgreSQL database (Klspl_Acs) in DBeaver,
-- export the single column as plain text (no header needed), and save the
-- result as  boolean_columns.txt  next to MsSqlToPg.exe (or next to app.py).
--
-- A name is listed only when it is BOOLEAN in EVERY table/view that declares
-- it. A name that is boolean in one table and integer in another is left out,
-- so the converter never guesses (e.g. `remarks` is boolean in
-- DocumentConfiguration but text elsewhere -- it will not be listed).
SELECT lower(column_name) AS column_name
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
GROUP BY lower(column_name)
HAVING count(*) FILTER (WHERE data_type = 'boolean') > 0
   AND count(*) FILTER (WHERE data_type <> 'boolean') = 0
ORDER BY 1;
