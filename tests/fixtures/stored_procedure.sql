CREATE PROCEDURE dbo.usp_GetUsers @Status INT
AS
BEGIN
    SET NOCOUNT ON;
    IF @Status IS NULL SET @Status = 1;
    SELECT * FROM Users WHERE Status = @Status;
END