SELECT COUNT(VD.vehicleid)
FROM #tmpVehicleDetails VD
INNER JOIN #tmpExpGateInParking EGIN ON EGIN.VehicleId = VD.VehicleId
WHERE ISNULL(EGIN.ThroughParking, CAST(0 AS BIT)) = CAST(0 AS BIT)
  AND ISNULL(VD.IsBackToTown, CAST(0 AS BIT)) = CAST(0 AS BIT);
