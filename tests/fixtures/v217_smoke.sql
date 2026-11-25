SELECT 
		P.ProductId AS [ProductId], 
		P.CategoryId AS [CategoryId], 
		G.Code AS [CategoryCode], 
		(G.Code + ' - ' + P.Title) AS [DisplayName], 
		P.IsActive AS [IsActive], 
		CASE WHEN ISNULL(P.IsActive, 'false') = 'true' THEN 'YES' ELSE 'NO' END AS ActiveLabel 
		INTO #temp 
		FROM Product P WITH(NOLOCK) 
		LEFT JOIN Category G WITH(NOLOCK) ON G.CategoryId = P.CategoryId 
		WHERE ISNULL(P.IsDeleted, 'false') = 'false' 
		AND P.TenantId = @TenantId 

		SELECT * INTO #ProductList FROM #temp 
		WHERE 
		(ISNULL('@searchedtext', '') = '') OR 
		( 
		ISNULL('@searchedtext', '') != '' AND 
		(ISNULL('@searchedfield', '') = 'Name' AND DisplayName = '@searchedtext') 
		); 

		SELECT *, 
		(SELECT COUNT(*) AS TotalRecords FROM #ProductList) AS TotalRecords 
		FROM #ProductList ORDER BY ProductId DESC; 

		DROP TABLE #ProductList; 
		DROP TABLE #temp; 
