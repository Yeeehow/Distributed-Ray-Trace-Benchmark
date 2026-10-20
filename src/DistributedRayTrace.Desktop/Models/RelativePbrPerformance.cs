using System.Globalization;
using DistributedRayTrace.Contracts.Enums;

namespace DistributedRayTrace.Desktop.Models;

public sealed record RelativePbrPerformance(string LocalDisplay,string ChangeDisplay,decimal? DistributedPercent,string Notice)
{
    public static RelativePbrPerformance Compare(CompletedPbrResult? local,CompletedPbrResult? distributed)
    {
        var baseline=local is {Mode:RenderMode.LocalAllCore,ElapsedNanoseconds:>0}?"100%":"—";
        RelativePbrPerformance No(string why)=>new(baseline,"—",null,why);
        if(local is null || distributed is null)return No("Complete both Local and Distributed runs to compare.");
        if(local.Mode!=RenderMode.LocalAllCore || distributed.Mode!=RenderMode.DistributedAllCore)return No("The completed modes differ from the comparison roles.");
        if(local.ElapsedNanoseconds<=0 || distributed.ElapsedNanoseconds<=0)return No("A positive measured duration is required.");
        if(local.WorkloadDigest!=distributed.WorkloadDigest || local.SceneId!=distributed.SceneId || local.SceneSha256!=distributed.SceneSha256
            || local.Width!=distributed.Width || local.Height!=distributed.Height || local.Spp!=distributed.Spp || local.Depth!=distributed.Depth
            || local.Seed!=distributed.Seed || local.OutputPolicy!=distributed.OutputPolicy)return No("Completed workloads are not equivalent.");
        var identicalFloat64=local.LinearSha256==distributed.LinearSha256;
        var identicalDisplay=local.Rgb8Sha256 is {Length:64} && local.Rgb8Sha256==distributed.Rgb8Sha256;
        if(!identicalFloat64 && (!identicalDisplay || local.Counters!=distributed.Counters))
            return No("Completed output images or ray counters differ.");
        if(local.TimingPolicy!=CompletedPbrResult.ContiguousTiming || distributed.TimingPolicy!=local.TimingPolicy)
            return No("Timing boundaries are not comparable.");
        var percent=100m*local.ElapsedNanoseconds/distributed.ElapsedNanoseconds;
        var change=decimal.Round(percent-100m,0,MidpointRounding.AwayFromZero);
        var display=(change>=0?"+":"")+change.ToString("0",CultureInfo.InvariantCulture)+"%";
        return new("100%",display,percent,(identicalFloat64?"Equivalent workloads and identical Float64 output. ":
            "Equivalent workloads, identical RGB8 output and ray counters; Float64 bytes differ across CPU/platform math. ")+
            "Contiguous execution including preparation and confirmed cleanup. Nearest integer, midpoint away from zero.");
    }
}
