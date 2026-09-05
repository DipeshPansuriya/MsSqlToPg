namespace Login_Command.LogIn.Queries.GetUserConfig;

public static class GetUserConfigQueries
{
    public static readonly string GetUserBranchContext = @"
        SELECT TOP 1
            UB.BranchId,
            UB.RoleId
        FROM Admin_UserBranchDetails UB WITH(NOLOCK)
        INNER JOIN OrgProduct OP WITH(NOLOCK)
            ON OP.OrgProdId = UB.OrgProdId
            AND ISNULL(OP.IsDeleted, 0) = 0
        WHERE UB.UserId = @UserId
          AND ISNULL(UB.DefaultBranch, 0) = 1
        ORDER BY UB.BranchId ASC";

    public static readonly string GetRightsFieldLabels = @"
        SELECT ML.FieldName,
            CAST((CASE WHEN RF.NotEditAccess = 'true' THEN 'true' ELSE 'false' END) AS BIT) NotEditAccess,
            @OrgProdId AS OrgProdId,
            ISNULL(MLD.parentTab,'') parentTab
        FROM Rights_FieldLabels RF WITH(NOLOCK)
        INNER JOIN Master_Localization ML WITH(NOLOCK) ON RF.LocalizationId = ML.LocalizationId
        WHERE RF.RoleId = @RoleId AND ISNULL(RF.IsButtonField,'false') = 'false'";

    public const string NotSql = "just a message, definitely not SQL";
    public static readonly string HasQuotes = @"SELECT ""Quoted"" FROM t WHERE a = 'x''y'";
}
