using DistributedRayTrace.Contracts.Enums;

namespace DistributedRayTrace.Contracts.Models;

public sealed record RenderError
{
    public string Code { get; }
    public string Message { get; }
    public RenderError(string code, string message)
    {
        Code = ContractGuard.Text(code, nameof(code));
        Message = ContractGuard.Text(message, nameof(message), 4096);
    }
}

public sealed record RenderResult
{
    public Guid RunId { get; }
    public RenderState TerminalState { get; }
    public long? ElapsedMilliseconds { get; }
    public RenderOrigin Origin { get; }
    public bool IsBenchmarkValid { get; }
    public RenderError? Error { get; }

    public RenderResult(Guid runId, RenderState terminalState, long? elapsedMilliseconds,
        RenderOrigin origin, bool isBenchmarkValid, RenderError? error = null)
    {
        ContractGuard.Require(runId != Guid.Empty && RenderProtocol.IsTerminal(terminalState), "Invalid terminal result.");
        ContractGuard.Require(Enum.IsDefined(origin), "Unknown result origin.");
        ContractGuard.Require(elapsedMilliseconds is null || elapsedMilliseconds >= 0
            && elapsedMilliseconds <= TimeSpan.MaxValue.Ticks / TimeSpan.TicksPerMillisecond, "Invalid elapsed time.");
        ContractGuard.Require(!isBenchmarkValid || origin == RenderOrigin.Real
            && terminalState == RenderState.Completed && elapsedMilliseconds > 0, "A Mock or incomplete result cannot be a valid benchmark.");
        ContractGuard.Require(terminalState == RenderState.Failed ? error is not null : error is null, "Error information belongs to failed results.");
        RunId = runId;
        TerminalState = terminalState;
        ElapsedMilliseconds = elapsedMilliseconds;
        Origin = origin;
        IsBenchmarkValid = isBenchmarkValid;
        Error = error;
    }
}
