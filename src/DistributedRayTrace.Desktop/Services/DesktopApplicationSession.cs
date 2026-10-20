using System.Windows.Threading;
using System.IO;
using DistributedRayTrace.Desktop.Services.Diagnostics;
using DistributedRayTrace.Desktop.Services.Cache;
using DistributedRayTrace.Desktop.Services.Dialogs;
using DistributedRayTrace.Desktop.Services.Preview;
using DistributedRayTrace.Desktop.Services.Rendering;
using DistributedRayTrace.Desktop.Services.Settings;
using DistributedRayTrace.Desktop.Services.SystemInfo;
using DistributedRayTrace.Desktop.ViewModels;

namespace DistributedRayTrace.Desktop.Services;

/// <summary>App-owned composition and lifetime. Real Python serial preview, no fallback.</summary>
public sealed class DesktopApplicationSession : IAsyncDisposable
{
    private Task? _shutdown;
    public PythonProcessRenderBackend Backend { get; }
    public RenderSessionController Sessions { get; }
    public MainViewModel ViewModel { get; }
    public ProgressiveFramePresenter Presenter { get; }
    public IDesktopDialogs Dialogs { get; }
    public IReferencePreviewSource Reference { get; }
    public MainWindow Window { get; }

    public DesktopApplicationSession(Dispatcher dispatcher, ISettingsStore settings,
        ISystemInfoProvider? systemInfo = null, IReferencePreviewSource? reference = null,
        ISceneInspectionService? inspection = null, IScenePathResolver? scenePaths = null,
        PythonRenderConfiguration? renderConfiguration = null)
    {
        Reference = reference ?? new EmbeddedReferencePreviewSource();
#if DEBUG
        var applicationDirectory=AppContext.BaseDirectory;
#else
        // IncludeAllContentForSelfExtract relocates AppContext.BaseDirectory into the cache.
        // Runtime resources remain adjacent to the original release EXE.
        var applicationDirectory=Path.GetDirectoryName(Environment.ProcessPath)??AppContext.BaseDirectory;
#endif
        Backend = new PythonProcessRenderBackend(renderConfiguration ?? PythonRenderConfiguration.Development(applicationDirectory));
        Sessions = new RenderSessionController(Backend);
        var dialogs = new WpfDesktopDialogs(settings,
            inspection ?? new PythonSceneInspectionService(applicationDirectory),
            scenePaths ?? new ApplicationScenePathResolver(applicationDirectory));
        Dialogs = dialogs;
        void Dispatch(Action action)
        {
            if (dispatcher.HasShutdownStarted) return;
            if (dispatcher.CheckAccess()) action();
            else dispatcher.BeginInvoke(action);
        }
        ViewModel = new MainViewModel(systemInfo ?? new WindowsSystemInfoProvider(), settings, dialogs,
            Sessions, Dispatch, dialogs.ShowHelp, new LocalRenderCache(), dialogs);
        Window = new MainWindow(ViewModel, Reference, dialogs, () => DisposeAsync().AsTask());
        dialogs.AttachOwner(Window);
        Presenter = new ProgressiveFramePresenter(Sessions,
            action => Window.Dispatch(action, DispatcherPriority.Background), Window.PresentFrame);
    }

    public ValueTask DisposeAsync() => new(_shutdown ??= ShutdownCoreAsync());

    private async Task ShutdownCoreAsync()
    {
        // MainViewModel borrows the controller and child presentation model; only this owner disposes them.
        var tasks = new[] { ViewModel.DisposeAsync().AsTask(),
            Presenter.DisposeAsync().AsTask(), Sessions.DisposeAsync().AsTask(), Dialogs.ShutdownAsync() };
        await Task.WhenAll(tasks);
    }
}
