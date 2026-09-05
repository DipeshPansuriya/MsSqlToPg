CREATE TABLE #tmpDateConversions (
    FromDateLocal DATE,
    FromDateUtc   timestamp,
    ToDateUtcExcl timestamp
);
INSERT INTO #tmpDateConversions (FromDateLocal, FromDateUtc)
SELECT d.FromDateLocal,
       CAST(d.FromDateLocal AS timestamp) - (fnGetLocalTime(CAST(d.FromDateLocal AS timestamp), 'India Standard Time') - CAST(d.FromDateLocal AS timestamp))
FROM (SELECT CAST('31-07-2026 00:00:00' AS date) AS FromDateLocal) d;
