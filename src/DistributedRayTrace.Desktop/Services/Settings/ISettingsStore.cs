using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.Services.Settings;

public sealed record SettingsLoadResult(ApplicationSettings Settings, string? Warning = null);
public sealed record SettingsSaveResult(bool Success, string? Error = null);

public interface ISettingsStore
{
    Task<SettingsLoadResult> LoadAsync(CancellationToken cancellationToken = default);
    Task<SettingsSaveResult> SaveAsync(ApplicationSettings settings, CancellationToken cancellationToken = default);
}
