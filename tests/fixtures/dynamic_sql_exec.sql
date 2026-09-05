CREATE TABLE #UserTarget (ID INT);
DECLARE @TableName NVARCHAR(50) = 'ActiveUsers';
DECLARE @DynamicSQL NVARCHAR(MAX);
SET @DynamicSQL = 'INSERT INTO #UserTarget SELECT UserID FROM ' + @TableName + ' WHERE Status = 1';
EXEC(@DynamicSQL);
SELECT * FROM #UserTarget;