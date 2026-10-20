using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Models;

namespace DistributedRayTrace.Contracts.Events;

public sealed record PbrCounters(long PrimaryRays,long SecondaryRays,long ShadowRays,long PathCount,long SurfaceInteractions,int SchemaVersion);
public sealed record PbrContribution(string Id,int Pid,long AcceptedSamples,long PrimaryRays,long SecondaryRays,long ShadowRays)
{
    [System.Text.Json.Serialization.JsonIgnore]
    public long TotalRays=>checked(PrimaryRays+SecondaryRays+ShadowRays);
}
