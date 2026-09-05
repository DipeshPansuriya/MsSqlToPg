SELECT u.ID, x.Total, TRY_CAST(u.Code AS INT) AS c
FROM Users u
CROSS APPLY (SELECT SUM(Amount) AS Total FROM Orders o WHERE o.UserID = u.ID) x;