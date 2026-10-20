using System.Windows;
using DistributedRayTrace.Desktop.Models;
using DistributedRayTrace.Desktop.Services.Settings;
using DistributedRayTrace.Desktop.Views;

namespace DistributedRayTrace.Desktop.Services.Dialogs;

public sealed class WpfSettingsDialog(Window owner, ISettingsStore store) : ISettingsDialog
{
    public ApplicationSettings? Show(ApplicationSettings current, string? notice)
    {
        var dialog = new SettingsWindow(new ViewModels.SettingsViewModel(store, current, notice)) { Owner = owner };
        return dialog.ShowDialog() == true ? dialog.SavedSettings : null;
    }
}
