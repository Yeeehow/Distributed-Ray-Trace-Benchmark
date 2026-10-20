using System.Windows;
using DistributedRayTrace.Desktop.Models;
using DistributedRayTrace.Desktop.Services.Diagnostics;
using DistributedRayTrace.Desktop.Services.Settings;
using DistributedRayTrace.Desktop.ViewModels;
using DistributedRayTrace.Desktop.Views;
using Microsoft.Win32;

namespace DistributedRayTrace.Desktop.Services.Dialogs;

/// <summary>Owns feature windows; each inspection window owns and shuts down its own ViewModel.</summary>
public sealed class WpfDesktopDialogs(ISettingsStore store, ISceneInspectionService inspection, IScenePathResolver paths) : IDesktopDialogs, ILocalCacheDialogs
{
    private Window? _owner;
    private readonly HashSet<Window> _windows = [];
    private SceneInspectionWindow? _inspection;
    private bool _closing;
    private Task? _shutdown;

    public void AttachOwner(Window owner)
    {
        if (_owner is not null) throw new InvalidOperationException("Dialog owner is already attached.");
        _owner = owner;
    }

    private T Track<T>(T window) where T : Window
    {
        window.Owner = _owner ?? throw new InvalidOperationException("Dialog owner is not attached.");
        _windows.Add(window);
        window.Closed += (_, _) => _windows.Remove(window);
        return window;
    }

    public ApplicationSettings? Show(ApplicationSettings current, string? notice)
    {
        if (_closing) return null;
        var viewModel = new SettingsViewModel(store, current, notice);
        var window = Track(new SettingsWindow(viewModel));
        return window.ShowDialog() == true ? viewModel.SavedSettings : null;
    }

    public void ShowHelp(HelpTopic topic)
    {
        if (!_closing) Track(new HelpWindow(topic)).ShowDialog();
    }

    public bool ConfirmClearLocalCache(string directory) => !_closing && MessageBox.Show(
        _owner!, "Permanently delete saved renders and run logs in:\n\n" + directory +
        "\n\nApplication settings and scene assets are kept. This cannot be undone.",
        "Clear Local Cache", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) == MessageBoxResult.Yes;

    public void ShowLocalCacheResult(string message, bool success)
    {
        if (!_closing) MessageBox.Show(_owner!, message, "Clear Local Cache", MessageBoxButton.OK,
            success ? MessageBoxImage.Information : MessageBoxImage.Warning);
    }

    public void ShowSceneInspection()
    {
        if (_closing) return;
        if (_inspection is not null) { _inspection.Activate(); return; }
        SceneInspectionWindow? window = null;
        var viewModel = new SceneInspectionViewModel(inspection, paths, () =>
        {
            var dialog = new OpenFileDialog { Filter = "GLB scenes (*.glb)|*.glb|All files (*.*)|*.*", CheckFileExists = true };
            return dialog.ShowDialog(window) == true ? dialog.FileName : null;
        });
        window = Track(new SceneInspectionWindow(viewModel));
        _inspection = window;
        window.Closed += (_, _) => { if (ReferenceEquals(_inspection, window)) _inspection = null; };
        window.Show();
    }

    public Task ShutdownAsync() => _shutdown ??= ShutdownCoreAsync();

    private async Task ShutdownCoreAsync()
    {
        _closing = true;
        var errors = new List<Exception>();
        foreach (var window in _windows.ToArray())
        {
            try
            {
                if (window is SceneInspectionWindow scene) await scene.ShutdownAsync();
                else if (window is SettingsWindow settings) await settings.ShutdownAsync();
                else window.Close();
            }
            catch (Exception error) { errors.Add(error); }
        }
        if (errors.Count != 0) throw new AggregateException("Some dialogs could not finish shutdown.", errors);
    }
}
