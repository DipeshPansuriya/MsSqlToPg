WITH RankedOrders AS (
    SELECT o.OrderID, o.CustomerID, o.OrderDate,
           ROW_NUMBER() OVER (PARTITION BY o.CustomerID ORDER BY o.OrderDate DESC) AS rn,
           ISNULL(o.Freight, 0) AS Freight
    FROM dbo.Orders o WITH (NOLOCK)
    WHERE o.OrderDate >= DATEADD(month, -6, GETDATE())
)
SELECT TOP 100 r.CustomerID,
       CONVERT(VARCHAR(10), r.OrderDate, 120) AS OrderDay,
       CHARINDEX('X', c.CompanyName) AS pos
FROM RankedOrders r
JOIN dbo.Customers c ON c.CustomerID = r.CustomerID
WHERE r.rn = 1
ORDER BY r.Freight DESC;