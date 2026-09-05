SELECT I.Id, I.InvoiceStatus
INTO #tmpInvoice
FROM qa_northwind_final.dbo.Inv_Invoice I WITH(NOLOCK)
WHERE ISNULL(I.IsDeleted, 0) = 0;
CREATE CLUSTERED INDEX IX_tmpInvDetails_Id ON #tmpInvoice(Id);
