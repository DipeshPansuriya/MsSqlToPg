SELECT MTP.TerminalPrintingId,
		MTP.TerminalZone[TerminalZoneId],
		MTP.IsActive
		FROM [@MasterdbName].[dbo].[Master_Terminal_Printing] MTP WITH(NOLOCK)
		WHERE ISNULL(MTP.IsDeleted,0)=0 AND MTP.TerminalPrintingId = @TerminalPrintingId

		SELECT CNSRTM_CD FROM [@MasterdbName].[dbo].[Master_Consurtium] Where CNSRTM_CD LIKE '%' + '@SearchString' + '%' --GROUP BY VALID_FM_DTTM
		SELECT Status FROM [@MasterdbName].[dbo].[Master_Consurtium] Where Status LIKE '%@SearchString%' GROUP BY Status
