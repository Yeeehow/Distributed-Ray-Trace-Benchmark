using System.IO;
using System.Windows;
using DistributedRayTrace.Desktop.Services;
using DistributedRayTrace.Desktop.Services.Settings;

namespace DistributedRayTrace.Desktop;

public partial class App : Application
{
    private DesktopApplicationSession? _session;
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        ISettingsStore settings;
        if (e.Args.Length == 0) settings = new JsonSettingsStore();
        else if (e.Args is ["--settings-root", var root] && Path.IsPathFullyQualified(root))
            settings = new JsonSettingsStore(root);
        else
        {
            MessageBox.Show("Usage: DistributedRayTrace.Desktop.exe [--settings-root <absolute directory>]",
                "Distributed Ray Trace — startup options", MessageBoxButton.OK, MessageBoxImage.Information);
            Shutdown(2);
            return;
        }
        try
        {
            _session = new DesktopApplicationSession(Dispatcher, settings);
            MainWindow = _session.Window;
            MainWindow.Show();
        }
        catch (Exception error)
        {
            System.Diagnostics.Trace.TraceError("Application startup failed: {0}", error);
            MessageBox.Show("The desktop application could not start. No alternate backend was selected.",
                "Distributed Ray Trace", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
        }
    }
}
