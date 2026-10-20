using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Contracts.Models;

namespace DistributedRayTrace.Contracts.Interfaces;

public interface IRenderBackend : IAsyncDisposable
{
    bool IsAvailable { get; }
    RenderOrigin Origin { get; }
    // Enumerating starts exactly one request. This token stops local observation, not remote work.
    IAsyncEnumerable<RenderEvent> StartAsync(RenderRequest request, CancellationToken observationCancellation = default);
    Task RequestCancellationAsync(Guid runId, CancellationToken commandCancellation = default);
}
