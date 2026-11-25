UPDATE c SET c.StateCode = S.StateCode FROM #tempCity c INNER JOIN [@AppDbName].[dbo].[Ref_State] S ON S.StateId = c.StateId
DECLARE @SearchVal NVARCHAR(25) SET @SearchVal = '@SearchString'
IF (@SearchVal = '') BEGIN SELECT MC.Name FROM [@AppDbName].[dbo].[Ref_Vendor] MC END
ELSE BEGIN SELECT MC.Name FROM [@AppDbName].[dbo].[Ref_Vendor] MC WHERE MC.Name LIKE '%' + @SearchVal + '%' END
