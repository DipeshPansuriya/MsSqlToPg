SELECT DISTINCT SI.StandingInstructionId,
COALESCE(STUFF((select distinct ', ' + CONVERT(TEXT,SHC.SHCId) FROM SISHCDetail SHC
WHERE SHC.SIShipmentId = SHCD.SIShipmentId for xml path('')),1,1,''),'ALL') as SHCList
FROM StandingInstruction SI
LEFT JOIN SISHCDetail SHCD ON SHCD.SIShipmentId = SI.Id;
