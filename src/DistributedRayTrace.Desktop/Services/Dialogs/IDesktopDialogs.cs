using DistributedRayTrace.Desktop.ViewModels;

namespace DistributedRayTrace.Desktop.Services.Dialogs;

public interface IDesktopDialogs : ISettingsDialog
{
    void ShowHelp(HelpTopic topic);
    void ShowSceneInspection();
    Task ShutdownAsync();
}
