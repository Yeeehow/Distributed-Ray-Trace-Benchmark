using System.ComponentModel;
using System.Windows;
using DistributedRayTrace.Desktop.Models;
using DistributedRayTrace.Desktop.ViewModels;

namespace DistributedRayTrace.Desktop.Views;

public partial class SettingsWindow : Window
{
    private readonly SettingsViewModel _viewModel;
    public ApplicationSettings? SavedSettings => _viewModel.SavedSettings;

    public SettingsWindow(SettingsViewModel viewModel)
    {
        InitializeComponent();
        _viewModel = viewModel;
        DataContext = viewModel;
        _viewModel.Saved += SettingsSaved;
        Closed += (_, _) => _viewModel.Saved -= SettingsSaved;
    }

    private void SettingsSaved(object? sender, EventArgs e) => DialogResult = true;
    private void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (!_viewModel.CanDismiss) e.Cancel = true;
    }
    public async Task ShutdownAsync()
    {
        if (_viewModel.SaveCommand.ExecutionTask is { } saving)
            await saving.WaitAsync(TimeSpan.FromSeconds(5));
        Close();
    }
}
