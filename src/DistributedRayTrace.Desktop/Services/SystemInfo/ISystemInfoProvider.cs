using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.Services.SystemInfo;

public interface ISystemInfoProvider
{
    Task<SystemInformation> GetSnapshotAsync(CancellationToken cancellationToken = default);
}
