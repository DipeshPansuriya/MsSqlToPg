SELECT
		C.EDI_DataExtractionConfigId AS [EDI_DataExtractionConfigId],
		C.MessageType AS [MessageType],
		M.Code AS [MessageTypedesc],
		(M.Code + ' - ' + C.Attribute) AS [Messagedesc],
		C.Version AS [Version],
		V.Code AS [VersionDesc],
		C.IsActive AS [IsActive],
		CASE WHEN ISNULL(C.IsActive, 'false') = 'true' THEN 'YES' ELSE 'NO' END AS IsActiveDesc
		INTO #temp
		FROM EDI_DataExtractionConfig C WITH(NOLOCK)
		LEFT JOIN EDI_MessageType M WITH(NOLOCK) ON M.EDI_MessageType_Id = C.MessageType
		LEFT JOIN EDI_Version V WITH(NOLOCK) ON V.EDI_Version_Id = C.Version
		WHERE ISNULL(C.IsDeleted, 'false') = 'false'
		AND C.OrgProdId = @OrgProdId
		AND C.Community_Admin_OrgId = @CommunityOrgId

		SELECT * INTO #DataExtract FROM #temp
		WHERE
		(ISNULL('@searchedtext', '') = '') OR
		(
		ISNULL('@searchedtext', '') != '' AND
		(ISNULL('@texttype', '') = 'DataExtraction' AND Messagedesc = '@searchedtext')
		);

		SELECT *,
		(SELECT COUNT(*) AS TotalRecords FROM #DataExtract) AS TotalRecords
		FROM #DataExtract ORDER BY EDI_DataExtractionConfigId DESC;

		DROP TABLE #DataExtract;
		DROP TABLE #temp;
