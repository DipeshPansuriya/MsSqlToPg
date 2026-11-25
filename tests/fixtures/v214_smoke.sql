SELECT P.PrinterId, 
		P.SiteArea[SiteAreaId], 
		P.IsActive 
		FROM [@AppDbName].[dbo].[Ref_Printer] P WITH(NOLOCK) 
		WHERE ISNULL(P.IsDeleted,0)=0 AND P.PrinterId = @PrinterId 

		SELECT GroupCode FROM [@AppDbName].[dbo].[Ref_Group] Where GroupCode LIKE '%' + '@SearchString' + '%' --GROUP BY CreatedOn 
		SELECT Status FROM [@AppDbName].[dbo].[Ref_Group] Where Status LIKE '%@SearchString%' GROUP BY Status 
