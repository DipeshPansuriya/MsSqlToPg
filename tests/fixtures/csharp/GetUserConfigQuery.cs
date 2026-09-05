using MediatR;

namespace Login_Command.LogIn.Queries.GetUserConfig;

public class GetUserConfigQuery : IRequest<UserConfigAndFieldLabelsDTO>
{
    public int UserId { get; set; }
    public int OrgProdId { get; set; }
}
