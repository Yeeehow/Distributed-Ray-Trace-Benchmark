using System.ComponentModel;
using System.Windows;
using DistributedRayTrace.Desktop.ViewModels;

namespace DistributedRayTrace.Desktop.Views;

public partial class SceneInspectionWindow : Window
{
    private readonly SceneInspectionViewModel _viewModel;
    private Task? _shutdown;
    private bool _canClose;
    public Exception? ShutdownError { get; private set; }

    public SceneInspectionWindow(SceneInspectionViewModel viewModel)
    {
        InitializeComponent();
        _viewModel = viewModel;
        DataContext = viewModel;
    }

    public Task ShutdownAsync() => _shutdown ??= ShutdownCoreAsync();
    private async Task ShutdownCoreAsync()
    {
        IsEnabled = false;
        try { await _viewModel.DisposeAsync(); }
        catch (Exception error) { ShutdownError = error; throw; }
        finally
        {
            _canClose = true;
            if (!Dispatcher.HasShutdownStarted) _ = Dispatcher.BeginInvoke(new Action(Close));
        }
    }
    private async void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (_canClose) return;
        e.Cancel = true;
        try { await ShutdownAsync(); }
        catch (Exception error) { System.Diagnostics.Trace.TraceError("Scene inspection shutdown failed: {0}", error); }
    }
    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}
