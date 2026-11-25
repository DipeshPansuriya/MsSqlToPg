SELECT COUNT(VD.assetid)
FROM #tmpAssetDetails VD
INNER JOIN #tmpOutEntry OE ON OE.AssetId = VD.AssetId
WHERE ISNULL(OE.ThroughLane, CAST(0 AS BIT)) = CAST(0 AS BIT)
  AND ISNULL(VD.IsReturned, CAST(0 AS BIT)) = CAST(0 AS BIT);
