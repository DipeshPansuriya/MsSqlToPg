CREATE TABLE #OutboundFeeSummary (
    FeeDesc NVARCHAR(MAX),
    AmountOutbound DECIMAL(18,2),
    FeeCode NVARCHAR(MAX)
);
INSERT INTO #tmpNowLocal (NowLocal)
SELECT CAST(GETUTCDATE() AT TIME ZONE 'UTC' AT TIME ZONE 'India Standard Time' AS DATETIME);
