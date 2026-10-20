using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.Services.Dialogs;

public interface ISettingsDialog
{
    ApplicationSettings? Show(ApplicationSettings current, string? notice);
}
