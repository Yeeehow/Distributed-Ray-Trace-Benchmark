using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using DistributedRayTrace.Contracts.Events;

namespace DistributedRayTrace.Desktop.Services.Rendering;

public sealed record PresentedFrame(Guid? RunId, long Sequence, BitmapSource? Image, string? Error);

/// <summary>One decoder, one newest pending frame and one coalesced UI publication.</summary>
public sealed class ProgressiveFramePresenter : IAsyncDisposable
{
    private readonly object _gate = new();
    private readonly RenderSessionController _sessions;
    private readonly Action<Action> _dispatch;
    private readonly Action<PresentedFrame> _present;
    private readonly Func<RenderFrame, CancellationToken, BitmapSource> _decode;
    private readonly CancellationTokenSource _lifetime = new();
    private Guid? _runId;
    private long _seenSequence;
    private RenderFrame? _pending;
    private PresentedFrame? _ready;
    private Task? _worker;
    private bool _publicationQueued;
    private bool _disposed;
    private Task? _disposal;

    public ProgressiveFramePresenter(RenderSessionController sessions, Action<Action> dispatch, Action<PresentedFrame> present,
        Func<RenderFrame, CancellationToken, BitmapSource>? decode = null)
    {
        _sessions = sessions;
        _dispatch = dispatch;
        _present = present;
        _decode = decode ?? DecodePng;
        _sessions.Changed += Sessions_Changed;
    }

    public int PendingFrameCount { get { lock (_gate) return _pending is null ? 0 : 1; } }
    public Task WhenIdleAsync() { lock (_gate) return _worker ?? Task.CompletedTask; }

    private void Sessions_Changed(object? sender, EventArgs e)
    {
        var publishReset = false;
        lock (_gate)
        {
            if (_disposed) return;
            // Read inside the presenter lock so overlapping invalidations cannot restore an older run.
            var snapshot = _sessions.Snapshot;
            if (_runId != snapshot.Request?.RunId)
            {
                _runId = snapshot.Request?.RunId;
                _seenSequence = 0;
                _pending = null;
                _ready = new(_runId, 0, null, null);
                publishReset = QueuePublicationLocked();
            }
            if (snapshot.LatestFrame is { } frame && frame.Sequence > _seenSequence)
            {
                _seenSequence = frame.Sequence;
                _pending = frame;
                _worker ??= Task.Run(DecodeLoop);
            }
        }
        if (publishReset) _dispatch(Publish);
    }

    private void DecodeLoop()
    {
        while (true)
        {
            RenderFrame frame;
            lock (_gate)
            {
                if (_disposed || _pending is null) { _worker = null; return; }
                frame = _pending;
                _pending = null;
            }
            PresentedFrame decoded;
            try
            {
                var image = _decode(frame, _lifetime.Token);
                if (!image.IsFrozen) image.Freeze();
                decoded = new(frame.RunId, frame.Sequence, image, null);
            }
            catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { continue; }
            catch (Exception) { decoded = new(frame.RunId, frame.Sequence, null, "Frame decode failed; keeping the last usable preview."); }
            var publish = false;
            lock (_gate)
            {
                if (_disposed || !IsCurrent(frame.RunId, frame.Sequence)) continue;
                _ready = decoded;
                publish = QueuePublicationLocked();
            }
            if (publish) _dispatch(Publish);
        }
    }

    private bool QueuePublicationLocked()
    {
        if (_publicationQueued) return false;
        _publicationQueued = true;
        return true;
    }

    private bool IsCurrent(Guid? runId, long sequence)
    {
        var snapshot = _sessions.Snapshot;
        return snapshot.Request?.RunId == runId && (sequence == 0 || snapshot.LatestFrame?.Sequence == sequence);
    }

    private void Publish()
    {
        lock (_gate)
        {
            _publicationQueued = false;
            var ready = _ready;
            _ready = null;
            if (_disposed || ready is null || !IsCurrent(ready.RunId, ready.Sequence)) return;
            _present(ready);
        }
    }

    public static BitmapSource DecodePng(RenderFrame frame, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        using var stream = new MemoryStream(frame.PngBytes, writable: false);
        var image = new BitmapImage();
        image.BeginInit();
        image.CacheOption = BitmapCacheOption.OnLoad;
        image.CreateOptions = BitmapCreateOptions.PreservePixelFormat;
        image.StreamSource = stream;
        image.EndInit();
        if (image.PixelWidth != frame.Width || image.PixelHeight != frame.Height) throw new FormatException("Decoded dimensions do not match metadata.");
        var converted = new FormatConvertedBitmap(image, PixelFormats.Bgr32, null, 0);
        var stride = checked(frame.Width * 4);
        var pixels = new byte[checked(stride * frame.Height)];
        converted.CopyPixels(pixels, stride, 0);
        var detached = BitmapSource.Create(frame.Width, frame.Height, 96, 96, PixelFormats.Bgr32, null, pixels, stride);
        detached.Freeze();
        token.ThrowIfCancellationRequested();
        return detached;
    }

    public ValueTask DisposeAsync()
    {
        lock (_gate) return new(_disposal ??= DisposeCoreAsync());
    }

    private async Task DisposeCoreAsync()
    {
        Task? worker;
        lock (_gate)
        {
            _disposed = true;
            _sessions.Changed -= Sessions_Changed;
            _pending = null;
            _ready = null;
            _lifetime.Cancel();
            worker = _worker;
        }
        var releaseLater = false;
        try
        {
            if (worker is not null) await worker.WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            releaseLater = true;
            _ = ReleaseAfterDecodeAsync(worker!);
            throw new TimeoutException("Frame decoding did not stop within five seconds. Publication is disabled; decoder termination is not confirmed.");
        }
        finally { if (!releaseLater) _lifetime.Dispose(); }
    }

    private async Task ReleaseAfterDecodeAsync(Task worker)
    {
        try { await worker.ConfigureAwait(false); }
        catch (Exception error) { System.Diagnostics.Trace.TraceError("Late frame decode failed: {0}", error); }
        finally { _lifetime.Dispose(); }
    }
}
