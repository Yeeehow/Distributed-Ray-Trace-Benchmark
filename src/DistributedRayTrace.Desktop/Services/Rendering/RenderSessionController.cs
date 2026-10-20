using DistributedRayTrace.Contracts;
using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Contracts.Interfaces;
using DistributedRayTrace.Contracts.Models;
using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.Services.Rendering;

public sealed record RenderSessionSnapshot(RenderRequest? Request, RenderState State, long LastSequence,
    RenderProgress? Progress, RenderFrame? LatestFrame, IReadOnlyList<RenderContribution> Contributions, RenderResult? Result,
    bool FrameIsProvisional=false, long PreviewSamples=0,
    RoomEvent? Room=null);

/// <summary>Owns one run's lifecycle and bounded accepted state. Contains no WPF or rendering work.</summary>
public sealed class RenderSessionController : IAsyncDisposable
{
    private readonly object _gate = new();
    private readonly IRenderBackend? _backend;
    private readonly CancellationTokenSource _shutdown = new();
    private RenderSessionSnapshot _snapshot = new(null, RenderState.Idle, 0, null, null, Array.Empty<RenderContribution>(), null);
    private Task? _observationTask;
    private Task _backendStartReady = Task.CompletedTask;
    private bool _observing;
    private bool _disposed;
    private Task? _disposal;
    private RoomEventAcceptance? _roomAcceptance;
    private readonly Dictionary<RenderMode,CompletedPbrResult> _lastSuccessful=[];
    private CompletedPbrResult? _mostRecentSuccessful;
    public CompletedPbrResult? MostRecentSuccessful {get {lock(_gate)return _mostRecentSuccessful;}}
    public IReadOnlyDictionary<RenderMode,CompletedPbrResult> LastSuccessfulResults
    {
        get {lock(_gate)return new System.Collections.ObjectModel.ReadOnlyDictionary<RenderMode,CompletedPbrResult>(
            new Dictionary<RenderMode,CompletedPbrResult>(_lastSuccessful));}
    }
    public bool IsPbrAvailable=>(_backend as PythonProcessRenderBackend)?.SupportsRoom==true;
    public bool IsPbrDistributedAvailable=>(_backend as PythonProcessRenderBackend)?.SupportsRoomDistributed==true;
    public string RoomAgentStatus=>(_backend as PythonProcessRenderBackend)?.RoomAgentStatus??"No room PBR backend";
    public IReadOnlyList<VerifiedRemoteNode> RoomRemoteNodes=>(_backend as PythonProcessRenderBackend)?.RoomRemoteNodes??Array.Empty<VerifiedRemoteNode>();

    // An invalidation notification: consumers read Snapshot, rather than applying queued old snapshots.
    public event EventHandler? Changed;
    public RenderSessionController(IRenderBackend? backend = null)
    {
        _backend=backend;
        if(backend is PythonProcessRenderBackend python)python.RemoteChanged+=OnRemoteChanged;
    }
    private void OnRemoteChanged(object? sender,EventArgs e)=>Changed?.Invoke(this,EventArgs.Empty);
    public IReadOnlyList<RemotePeerState> RemotePeers=>(_backend as PythonProcessRenderBackend)?.RemotePeers??[];
    public Task ImportRoomAgentsAsync(string path,CancellationToken token=default)=>(_backend as PythonProcessRenderBackend)?.ImportRoomAgentsAsync(path,token)??Task.CompletedTask;
    public void SelectRoomPeer(string id,bool selected)=>(_backend as PythonProcessRenderBackend)?.SelectRoomPeer(id,selected);
    public Task ConnectRoomAgentsAsync(int width,int height,CancellationToken token=default)=>(_backend as PythonProcessRenderBackend)?.ConnectRoomAgentsAsync(width,height,token)??Task.CompletedTask;
    public Task DisconnectRoomAgentsAsync()=>(_backend as PythonProcessRenderBackend)?.DisconnectRoomAgentsAsync()??Task.CompletedTask;
    public bool HasBackend => _backend is not null;
    public RenderOrigin? BackendOrigin => _backend?.Origin;
    public bool IsBackendAvailable => _backend?.IsAvailable == true;
    public bool IsLocalParallelAvailable => _backend is PythonProcessRenderBackend { SupportsLocalParallel:true };
    public Task RefreshAgentsAsync(CancellationToken token=default)=>(_backend as PythonProcessRenderBackend)?.ProbeAgentsAsync(token)??Task.CompletedTask;
    public RenderSessionSnapshot Snapshot { get { lock (_gate) return _snapshot; } }
    public bool CanStart { get { lock (_gate) return !_disposed && !_observing && CanCreate(_snapshot.State); } }
    private static bool CanCreate(RenderState state) => state == RenderState.Idle || RenderProtocol.IsTerminal(state);

    public RenderRequest CreateSession(ApplicationSettings settings, RenderMode mode)
    {
        RenderRequest request;
        lock (_gate) request = CreateSessionLocked(settings, mode);
        Changed?.Invoke(this, EventArgs.Empty);
        return request;
    }

    private RenderRequest CreateSessionLocked(ApplicationSettings settings, RenderMode mode)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_observing || !CanCreate(_snapshot.State)) throw new InvalidOperationException("A session is already active.");
        ArgumentNullException.ThrowIfNull(settings);
        if(!settings.IsValid)throw new ArgumentException("Choose a supported PBR resolution in Settings.");
        if(mode==RenderMode.DistributedAllCore && !IsPbrDistributedAvailable)throw new ArgumentException("Selected authenticated room PBR Agents are required.");
        var request = new RenderRequest(RenderProtocol.RoomPbrVersion, Guid.NewGuid(), RenderProtocol.BenchmarkSceneId,
            mode, settings.Width, settings.Height, null,PbrConfiguration.Room(settings.Width,settings.Height));
        _roomAcceptance=new(request.RunId,request.Width,request.Height,request.Mode);
        _snapshot = new(request, RenderState.Preparing, 0, null, null, Array.Empty<RenderContribution>(), null);
        return request;
    }

    public Task StartAsync(ApplicationSettings settings, RenderMode mode, CancellationToken observationCancellation = default)
    {
        Task task;
        lock (_gate)
        {
            if (!IsBackendAvailable) throw new InvalidOperationException("No available backend is registered.");
            var request = CreateSessionLocked(settings, mode);
            _observing = true;
            var linked = CancellationTokenSource.CreateLinkedTokenSource(_shutdown.Token, observationCancellation);
            var starting = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _backendStartReady = starting.Task;
            task = _observationTask = ObserveAsync(request, linked, starting);
        }
        Changed?.Invoke(this, EventArgs.Empty);
        return task;
    }

    public Task StartPreviewAsync(int width, int height, CancellationToken observationCancellation = default) =>
        StartAsync(new ApplicationSettings { Width = width, Height = height }, RenderMode.CpuSerialPreview, observationCancellation);
    public Task StartLocalParallelAsync(CancellationToken observationCancellation = default) =>
        StartLocalParallelAsync(160,90,observationCancellation);
    public Task StartLocalParallelAsync(int width,int height,CancellationToken observationCancellation=default)=>
        StartAsync(new ApplicationSettings {Width=width,Height=height},RenderMode.LocalAllCore,observationCancellation);
    public Task StartDistributedAsync(CancellationToken observationCancellation=default)=>
        StartDistributedAsync(160,90,observationCancellation);
    public Task StartDistributedAsync(int width,int height,CancellationToken observationCancellation=default)=>
        StartAsync(new ApplicationSettings {Width=width,Height=height},RenderMode.DistributedAllCore,observationCancellation);

    private async Task ObserveAsync(RenderRequest request, CancellationTokenSource linked, TaskCompletionSource<bool> starting)
    {
        using var observation = linked;
        // StartAsync publishes its task/guard under the lock. Never invoke subscribers while that lock is held.
        await Task.Yield();
        try
        {
            await using var events = _backend!.StartAsync(request, linked.Token).GetAsyncEnumerator(linked.Token);
            var next = events.MoveNextAsync();
            // Enter the backend iterator before sending a cancellation queued during Preparing.
            starting.TrySetResult(true);
            while (await next.ConfigureAwait(false))
            {
                TryAcceptEvent(events.Current);
                if (RenderProtocol.IsTerminal(Snapshot.State)) break;
                next = events.MoveNextAsync();
            }
            FailActive(request.RunId, "stream_ended", "The backend event stream ended without a terminal event.");
        }
        catch (OperationCanceledException) when (linked.IsCancellationRequested)
        {
            FailActive(request.RunId, "observation_cancelled", "Local observation stopped; backend cancellation is not confirmed.");
        }
        catch (Exception error)
        {
            var text="The backend event stream failed: "+error.Message;
            FailActive(request.RunId, "backend_error", text[..Math.Min(4000,text.Length)]);
        }
        finally
        {
            starting.TrySetResult(true);
            lock (_gate) _observing = false;
            Changed?.Invoke(this, EventArgs.Empty);
        }
    }

    public bool TryAcceptEvent(RenderEvent message)
    {
        ArgumentNullException.ThrowIfNull(message);
        lock (_gate)
        {
            var current = _snapshot;
            if (_disposed || current.Request is not { } request || RenderProtocol.IsTerminal(current.State)
                || message.ProtocolVersion != request.ProtocolVersion || message.RunId != request.RunId
                || message.Sequence <= current.LastSequence) return false;
            var next = current;
            if(request.ProtocolVersion==5)
            {
                if(message is RoomEvent {Phase:"preview" or "frame",PngBytes:null})return false;
                if(_roomAcceptance?.Accept(message)!=true)return false;
                if(message is RoomEvent room)
                {
                    var image=room.PngBytes is null?current.LatestFrame:new RenderFrame(5,room.RunId,room.Sequence,
                        room.Width,room.Height,ImageEncoding.Png,room.PngBytes);
                    var entries=room.Contributions.Select(c=>new RenderContribution(5,room.RunId,room.Sequence,c.Id,
                        ContributorType.SoftwareWorker,c.TotalRays)).ToArray();
                    var state=_roomAcceptance.State;
                    if(current.State==RenderState.Cancelling && !RenderProtocol.IsTerminal(state))state=RenderState.Cancelling;
                    var result=RenderProtocol.IsTerminal(state)?new RenderResult(room.RunId,state,room.ElapsedMilliseconds,
                        RenderOrigin.Real,false,room.Error):null;
                    next=current with {State=state,Progress=new RenderProgress(5,room.RunId,room.Sequence,room.AcceptedSamples,room.TotalSamples,null),
                        LatestFrame=image,FrameIsProvisional=room.Phase=="preview" || room.Phase!="frame" && current.FrameIsProvisional,
                        Contributions=entries,Result=result,Room=room};
                }
            }
            else return false;
            _snapshot = next with { LastSequence = message.Sequence };
            if(CompletedPbrResult.From(_snapshot) is {} completed)
            { _lastSuccessful[completed.Mode]=completed;_mostRecentSuccessful=completed; }
        }
        Changed?.Invoke(this, EventArgs.Empty);
        return true;
    }

    public Task RequestCancellationAsync(CancellationToken commandCancellation = default)
    {
        Guid runId;
        Task starting;
        lock (_gate)
        {
            if (_disposed || _snapshot.State is not (RenderState.Preparing or RenderState.Rendering)) return Task.CompletedTask;
            runId = _snapshot.Request!.RunId;
            starting = _backendStartReady;
            _snapshot = _snapshot with { State = RenderState.Cancelling };
        }
        Changed?.Invoke(this, EventArgs.Empty);
        return _backend is null ? Task.CompletedTask : SendCancellationAsync(runId, starting, commandCancellation);
    }

    private async Task SendCancellationAsync(Guid runId, Task starting, CancellationToken cancellationToken)
    {
        try
        {
            await starting.WaitAsync(cancellationToken).ConfigureAwait(false);
            lock (_gate)
                if (_disposed || _snapshot.Request?.RunId != runId || RenderProtocol.IsTerminal(_snapshot.State)) return;
            await _backend!.RequestCancellationAsync(runId, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception) { FailActive(runId, "cancel_command_failed", "Cancellation could not be confirmed; the backend may still be running."); }
    }

    private void FailActive(Guid runId, string code, string message)
    {
        lock (_gate)
        {
            if (_disposed || _snapshot.Request?.RunId != runId || RenderProtocol.IsTerminal(_snapshot.State)) return;
            var result = new RenderResult(runId, RenderState.Failed, null, _backend?.Origin ?? RenderOrigin.Mock,
                false, new RenderError(code, message));
            // A local transport failure is not assigned a fabricated backend sequence number.
            _snapshot = _snapshot with { State = RenderState.Failed, Result = result };
        }
        Changed?.Invoke(this, EventArgs.Empty);
    }

    public ValueTask DisposeAsync()
    {
        lock (_gate) return new(_disposal ??= DisposeCoreAsync());
    }

    private async Task DisposeCoreAsync()
    {
        Guid? cancelRun;
        Task? observing;
        Task starting;
        lock (_gate)
        {
            _disposed = true;
            // Observation cancellation or a local failure does not prove backend work stopped.
            cancelRun = _snapshot.State is not (RenderState.Completed or RenderState.Cancelled)
                ? _snapshot.Request?.RunId : null;
            observing = _observationTask;
            starting = _backendStartReady;
        }
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(Snapshot.Request?.Mode==RenderMode.DistributedAllCore?12:5));
        var errors = new List<Exception>();
        async Task Attempt(Func<Task> operation, string phase)
        {
            Task? pending = null;
            try { pending = operation(); await pending.WaitAsync(timeout.Token).ConfigureAwait(false); }
            catch (Exception error)
            {
                errors.Add(new InvalidOperationException(phase + "; backend termination is not confirmed.", error));
                if (pending is not null) _ = ObserveLateCleanupAsync(pending);
            }
        }
        if (_backend is not null && cancelRun.HasValue)
        {
            await Attempt(() => starting, "Waiting for backend startup").ConfigureAwait(false);
            if (starting.IsCompletedSuccessfully)
                await Attempt(() => _backend.RequestCancellationAsync(cancelRun.Value, timeout.Token),
                    "Requesting backend cancellation").ConfigureAwait(false);
        }
        _shutdown.Cancel();
        if (_backend is not null)
            await Attempt(() => _backend.DisposeAsync().AsTask(), "Disposing backend").ConfigureAwait(false);
        if (observing is not null)
            await Attempt(() => observing, "Ending event observation").ConfigureAwait(false);
        if(_backend is PythonProcessRenderBackend python)python.RemoteChanged-=OnRemoteChanged;
        _shutdown.Dispose();
        if (errors.Count != 0) throw new AggregateException("Render-session cleanup failed.", errors);
    }

    private static async Task ObserveLateCleanupAsync(Task pending)
    {
        try { await pending.ConfigureAwait(false); }
        catch (Exception error) { System.Diagnostics.Trace.TraceError("Late backend cleanup failed: {0}", error); }
    }
}
