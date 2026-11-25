UPDATE c SET c.StateCode = S.StateCode FROM #tempCity c INNER JOIN [@MasterdbName].[dbo].[Master_State] S ON S.StateId = c.StateId
DECLARE @SearchVal NVARCHAR(25) SET @SearchVal = '@SearchString'
IF (@SearchVal = '') BEGIN SELECT MC.Name FROM [@MasterdbName].[dbo].[Master_Airline] MC END
ELSE BEGIN SELECT MC.Name FROM [@MasterdbName].[dbo].[Master_Airline] MC WHERE MC.Name LIKE '%' + @SearchVal + '%' END
