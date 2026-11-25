IF OBJECT_ID('tempdb..#tmp') IS NOT NULL DROP TABLE #tmp;
SELECT a, FORMAT(Amount, 'N2') AS Amt, DATEDIFF(day, CreatedDate, GETDATE()) AS AgeDays
INTO #tmp
FROM Orders WITH (NOLOCK)
WHERE IsDeleted = 0;
SELECT * FROM #tmp;
