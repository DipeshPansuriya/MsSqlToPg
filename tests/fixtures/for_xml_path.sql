SELECT DISTINCT O.OrderId, 
COALESCE(STUFF((select distinct ', ' + CONVERT(TEXT,T.TagId) FROM OrderTag T 
WHERE T.LineId = L.LineId for xml path('')),1,1,''),'ALL') as TagList 
FROM OrderHeader O 
LEFT JOIN OrderTag L ON L.LineId = O.Id; 
