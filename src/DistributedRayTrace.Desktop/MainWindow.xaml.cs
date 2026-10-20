using System.ComponentModel;
using System.Windows;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using DistributedRayTrace.Desktop.Services.Dialogs;
using DistributedRayTrace.Desktop.Services.Preview;
using DistributedRayTrace.Desktop.Services.Rendering;
using DistributedRayTrace.Desktop.ViewModels;

namespace DistributedRayTrace.Desktop;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel;
    private readonly IReferencePreviewSource _referenceSource;
    private readonly IDesktopDialogs _dialogs;
    private readonly Func<Task> _shutdown;
    private readonly CancellationTokenSource _lifetime = new();
    private BitmapSource? _reference;
    private bool _hasProgressiveImage;
    private bool _closing;
    private bool _closed;
    private bool _cleanupComplete;
    private Task? _initialization;
    public Exception? ShutdownError { get; private set; }

    public MainWindow(MainViewModel viewModel, IReferencePreviewSource referenceSource,
        IDesktopDialogs dialogs, Func<Task> shutdown)
    {
        InitializeComponent();
        _viewModel = viewModel;
        _referenceSource = referenceSource;
        _dialogs = dialogs;
        _shutdown = shutdown;
        DataContext = viewModel;
    }

    internal void Dispatch(Action action, DispatcherPriority priority)
    {
        if (_closing || _closed || Dispatcher.HasShutdownStarted) return;
        if (Dispatcher.CheckAccess()) action();
        else Dispatcher.BeginInvoke(priority, new Action(() => { if (!_closing && !_closed) action(); }));
    }

    public Task InitializeAsync() => _initialization ??= InitializeCoreAsync();
    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        try { await InitializeAsync(); }
        catch (Exception error)
        {
            System.Diagnostics.Trace.TraceError("Application initialization failed: {0}", error);
            if (!_closing && !_closed) _viewModel.ReportLifecycleError("Application initialization could not be completed.");
        }
    }

    private Task InitializeCoreAsync() => Task.WhenAll(_viewModel.InitializeAsync(), LoadReferencePreviewAsync());
    private async Task LoadReferencePreviewAsync()
    {
        try
        {
            var reference = await _referenceSource.LoadAsync(_lifetime.Token);
            if (_closing || _closed) return;
            _reference = reference;
            if (!_hasProgressiveImage) ReferencePreview.Source = reference;
            _viewModel.SetStaticPreviewAvailable(true);
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { }
        catch (Exception)
        {
            if (!_closing && !_closed) _viewModel.SetStaticPreviewAvailable(false);
        }
    }

    internal void PresentFrame(PresentedFrame frame)
    {
        if (_closing || _closed) return;
        if (frame.Sequence == 0)
        {
            _hasProgressiveImage = false;
            ReferencePreview.Source = _reference;
        }
        else if (frame.Image is { } image)
        {
            _hasProgressiveImage = true;
            ReferencePreview.Source = image;
            if(_viewModel.Session.Request is {Pbr:not null} request && Environment.GetEnvironmentVariable("DRT_PBR_UI_EVIDENCE") is {} folder)
            {
                try
                {
                    if(!System.IO.Path.IsPathFullyQualified(folder))throw new System.IO.IOException("Absolute UI evidence directory required.");
                    System.IO.Directory.CreateDirectory(folder);
                    var rgb=new System.Windows.Media.Imaging.FormatConvertedBitmap(image,System.Windows.Media.PixelFormats.Rgb24,null,0);
                    var bytes=new byte[rgb.PixelWidth*rgb.PixelHeight*3];rgb.CopyPixels(bytes,rgb.PixelWidth*3,0);
                    var record=new{runId=request.RunId,sequence=frame.Sequence,width=rgb.PixelWidth,height=rgb.PixelHeight,
                        rgb8Sha256=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(bytes)).ToLowerInvariant(),
                        windowVisible=IsVisible,sourceAssigned=ReferenceEquals(ReferencePreview.Source,image),
                        executable=Environment.ProcessPath,utc=DateTimeOffset.UtcNow};
                    using var file=new System.IO.FileStream(System.IO.Path.Combine(folder,$"{request.RunId:D}.display.{frame.Sequence}.json"),System.IO.FileMode.CreateNew);
                    System.Text.Json.JsonSerializer.Serialize(file,record);
                }
                catch(Exception error){_viewModel.ReportLifecycleError("PBR display evidence failed: "+error.Message);}
            }
        }
        _viewModel.SetFramePresentation(frame.RunId, frame.Sequence, frame.Error);
    }

    private async void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (_cleanupComplete) return;
        e.Cancel = true;
        if (_closing) return;
        _closing = true;
        IsEnabled = false;
        _lifetime.Cancel();
        try
        {
            await Task.WhenAll(_shutdown(), (_initialization ?? Task.CompletedTask).WaitAsync(TimeSpan.FromSeconds(5)));
        }
        catch (Exception error)
        {
            ShutdownError = error;
            _viewModel.ReportLifecycleError("Application shutdown encountered an error; backend cleanup is not confirmed.");
            System.Diagnostics.Trace.TraceError("Application shutdown failed: {0}", error);
        }
        finally
        {
            _cleanupComplete = true;
            _lifetime.Dispose();
            // Leave the first Closing event before closing again, including synchronous cleanup.
            if (!Dispatcher.HasShutdownStarted) _ = Dispatcher.BeginInvoke(new Action(Close));
        }
    }

    private void Window_Closed(object? sender, EventArgs e) => _closed = true;
    private void OpenSceneInspection_Click(object sender, RoutedEventArgs e)
    {
        if (!_closing && !_closed) _dialogs.ShowSceneInspection();
    }
    private DistributedRayTrace.Desktop.Views.RemoteWindow? _remoteWindow;
    private void OpenRemote_Click(object sender,RoutedEventArgs e)
    {
        if(_closing || _closed)return;
        if(_remoteWindow is null)
        {
            _remoteWindow=new(_viewModel) {Owner=this};
            _remoteWindow.Closed+=(_,_)=>_remoteWindow=null;
            _remoteWindow.Show();
        }
        else _remoteWindow.Activate();
    }
}
