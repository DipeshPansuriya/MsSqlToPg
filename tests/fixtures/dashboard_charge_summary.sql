CREATE TABLE #ExportChargeSummary (
    ChargeDesc NVARCHAR(MAX),
    AmountExport DECIMAL(18,2),
    ChargeCode NVARCHAR(MAX)
);
INSERT INTO #tmpNowLocal (NowLocal)
SELECT CAST(GETUTCDATE() AT TIME ZONE 'UTC' AT TIME ZONE 'India Standard Time' AS DATETIME);
