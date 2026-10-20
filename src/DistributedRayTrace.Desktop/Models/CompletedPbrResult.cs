using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Desktop.Services.Rendering;

namespace DistributedRayTrace.Desktop.Models;

/// <summary>One validated completed result, independent of transient run state.</summary>
public sealed record CompletedPbrResult(Guid RunId,RenderMode Mode,string SceneId,string SceneSha256,string WorkloadDigest,
    int Width,int Height,int Spp,int Depth,long Seed,long ElapsedNanoseconds,long AcceptedSamples,string LinearSha256,
    string OutputPolicy,string TimingPolicy,DateTimeOffset CompletedUtc,string ResourceProfile,PbrCounters Counters,
    IReadOnlyList<PbrContribution> Contributions,string? Rgb8Sha256=null)
{
    public const string ContiguousTiming="drt.room_frontend_contiguous_execution.v1";
    public string ElapsedText=>(ElapsedNanoseconds/1e9).ToString("0.000",System.Globalization.CultureInfo.InvariantCulture)+" s";
    public string ScoreText=>ElapsedNanoseconds>0
        ?((System.Numerics.BigInteger)AcceptedSamples*1_000_000_000/ElapsedNanoseconds).ToString(System.Globalization.CultureInfo.InvariantCulture):"—";
    public string Detail=>$"{Width} × {Height} · {Spp} SPP · depth {Depth} · {CompletedUtc:yyyy-MM-dd HH:mm:ss} UTC";
    public static CompletedPbrResult? From(RenderSessionSnapshot s)
    {
        if(s.State!=RenderState.Completed || s.Request is not {ProtocolVersion:5,Pbr:{} p} r
            || s.Room is not {Phase:"completed",CoverageComplete:true,RenderNanoseconds:>0,Counters:{} c} e
            || e.RunId!=r.RunId || s.Result is not {TerminalState:RenderState.Completed,Origin:RenderOrigin.Real}
            || e.AcceptedSamples!=(long)r.Width*r.Height*p.TargetSpp || e.LinearSha256 is not {Length:64})return null;
        return new(r.RunId,r.Mode,r.SceneId,p.ResourceSha256,p.WorkloadDigest,r.Width,r.Height,p.TargetSpp,p.MaxDepth,p.Seed,
            e.RenderNanoseconds.Value,e.AcceptedSamples,e.LinearSha256,p.OutputPolicy,ContiguousTiming,DateTimeOffset.UtcNow,
            $"drt.room_admission.v2; 512 MiB per Worker; {e.Contributions.Count} accepted logical Workers; mode {r.Mode}",c,
            Array.AsReadOnly(e.Contributions.ToArray()),e.Rgb8Sha256);
    }
}
