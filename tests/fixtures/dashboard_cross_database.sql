SELECT I.Id, I.BillStatus
INTO #tmpBill
FROM qa_app_final.dbo.Fin_Bill I WITH(NOLOCK)
WHERE ISNULL(I.IsDeleted, 0) = 0;
CREATE CLUSTERED INDEX IX_tmpInvDetails_Id ON #tmpBill(Id);
