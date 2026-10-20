using System.Diagnostics;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Security.Cryptography;
using System.Windows.Media.Imaging;
using System.Threading.Channels;
using DistributedRayTrace.Contracts;
using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Contracts.Interfaces;
using DistributedRayTrace.Contracts.Models;
using DistributedRayTrace.Contracts.Serialization;
using DistributedRayTrace.Desktop.Services.Diagnostics;
using DistributedRayTrace.Desktop.Services.Cache;

namespace DistributedRayTrace.Desktop.Services.Rendering;

public sealed record VerifiedRemoteNode(string NodeId,int AvailableWorkers,int AgentPid);

public sealed record PythonRenderConfiguration(string Executable,string Scene,string OutputDirectory,
    IReadOnlyList<string>? HostPrefix=null,int ReadinessSeconds=10,int GraceMilliseconds=1500,
    string? RoomScene=null,string? RoomAgentsFile=null,int RoomRenderSeconds=10840)
{
    public static PythonRenderConfiguration Development(string applicationDirectory)
    {
#if DEBUG
        DirectoryInfo? directory=new(applicationDirectory);
        while(directory is not null && !File.Exists(Path.Combine(directory.FullName,"DistributedRayTrace","DistributedRayTrace.sln")))directory=directory.Parent;
        var executable=Environment.GetEnvironmentVariable("DRT_PYTHON_EXECUTABLE")??PythonSceneInspectionAdapter.ResolveInterpreter(applicationDirectory);
        var scene=Environment.GetEnvironmentVariable("DRT_ROOM_SCENE_PATH")??(directory is null?Path.Combine(applicationDirectory,"assets","shogi_match_room_v3.glb"):
            Path.Combine(directory.FullName,"assets","PyRenderAssets","shogi_match_room_v3.glb"));
        var output=Environment.GetEnvironmentVariable("DRT_RENDER_OUTPUT")??LocalRenderCache.DefaultDirectory;
        return new(executable,scene,output,RoomScene:scene,RoomAgentsFile:Environment.GetEnvironmentVariable("DRT_ROOM_AGENTS_FILE"));
#else
        var scene=Path.Combine(applicationDirectory,"assets","shogi_match_room_v3.glb");
        return new(Path.Combine(applicationDirectory,"backend","drt-backend.exe"),scene,
            LocalRenderCache.DefaultDirectory,
            new[]{"--ipc"},RoomScene:scene,RoomAgentsFile:Environment.GetEnvironmentVariable("DRT_ROOM_AGENTS_FILE"));
#endif
    }
}

/// <summary>Transport/process owner only. The existing controller owns accepted UI state.</summary>
public sealed partial class PythonProcessRenderBackend(PythonRenderConfiguration configuration) : IRenderBackend
{
    private readonly object _gate = new();
    private readonly SemaphoreSlim _write = new(1,1);
    private OwnedPythonProcess? _child;
    private Guid? _run;
    private bool _cancel;
    private bool _disposed;
    private Task? _pump;
    private Task? _stop;
    private CancellationTokenSource _lifetime = new();
    private bool _started;
    private RenderMode _activeMode;
    private int StopGraceMilliseconds=>_activeMode==RenderMode.DistributedAllCore?Math.Max(10000,configuration.GraceMilliseconds):
        _protocolVersion==5?Math.Max(7000,configuration.GraceMilliseconds):configuration.GraceMilliseconds;
    private int _protocolVersion;
    public bool IsAvailable => !_disposed;
    public RenderOrigin Origin => RenderOrigin.Real;
    public int? LauncherPid { get; private set; }
    public int? ComputationPid { get; private set; }
    public int[] LastOwnedPids { get; private set; } = [];
    public bool TerminationVerified { get; private set; }
    public bool ForcedTermination { get; private set; }
    public int? ProcessExitCode { get; private set; }
    public string Diagnostics { get; private set; } = "";
    public bool SupportsLocalParallel=>SupportsRoom;
    public Task ProbeAgentsAsync(CancellationToken token=default)=>ProbeRoomAgentsAsync(token);
    private static async Task<string> DrainProbeErrorAsync(StreamReader reader,CancellationToken token)
    { var buffer=new char[1024];var text=new StringBuilder();int count;while((count=await reader.ReadAsync(buffer.AsMemory(),token))>0){if(text.Length+count>16384)throw new IOException("Probe diagnostic limit exceeded.");text.Append(buffer,0,count);}return text.ToString(); }
    public double BackendStartupMilliseconds { get; private set; }

    public async IAsyncEnumerable<RenderEvent> StartAsync(RenderRequest request, [EnumeratorCancellation] CancellationToken observationCancellation=default)
    {
        if(request.ProtocolVersion!=5)throw new ArgumentException("Only original-room PBR frontend v5 is active.");
        ValidateRoomStart(request);
        using var cacheLease=LocalRenderCache.AcquireForOutput(configuration.OutputDirectory);
        var channel=Channel.CreateBounded<RenderEvent>(new BoundedChannelOptions(64) { FullMode=BoundedChannelFullMode.Wait,SingleWriter=true,SingleReader=true });
        if(request.ProtocolVersion==5 && request.Mode==RenderMode.DistributedAllCore)
        {
            _activeRoomAgentsFile=SelectedRoomTopology(request.RunId,true);
            await SuspendRoomMonitorAsync().ConfigureAwait(false);
        }
        lock(_gate)
        {
            ObjectDisposedException.ThrowIf(_disposed,this);
            if(_run is not null) throw new InvalidOperationException("One render stream at a time.");
            _lifetime.Dispose();_lifetime=new CancellationTokenSource();
            _run=request.RunId;_activeMode=request.Mode;_protocolVersion=request.ProtocolVersion;_cancel=false;_started=false;_stop=null;ComputationPid=null;TerminationVerified=false;ForcedTermination=false;ProcessExitCode=null;
            LauncherPid=null;LastOwnedPids=[];Diagnostics="";BackendStartupMilliseconds=0;
            _pump=Task.Run(()=>PumpAsync(request,channel));
        }
        try
        {
            await foreach(var message in channel.Reader.ReadAllAsync(observationCancellation).ConfigureAwait(false)) yield return message;
        }
        finally
        {
            _lifetime.Cancel();
            await StopAsync().ConfigureAwait(false);
            if(_pump is not null) await _pump.WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
            lock(_gate) _run=null;
            lock(_remoteGate) {_renderConnections.Clear();_activeRoomNodes=[];_activeRoomAgentsFile=null;}
            if(request.Mode==RenderMode.DistributedAllCore)await RestoreRoomMonitorAsync(request.Width,request.Height).ConfigureAwait(false);
            RemoteChanged?.Invoke(this,EventArgs.Empty);
        }
    }

    private async Task PumpAsync(RenderRequest request, Channel<RenderEvent> channel)
    {
        Exception? failure=null;
        Task? stderr=null;
        try
        {
            using var readiness=new CancellationTokenSource(TimeSpan.FromSeconds(configuration.ReadinessSeconds));
            var startup=Stopwatch.StartNew();
            using var linked=CancellationTokenSource.CreateLinkedTokenSource(readiness.Token,_lifetime.Token);
            var arguments=(configuration.HostPrefix ?? new[]{"-I","-B","-X","utf8","-u","-m","drt_backend.ipc"}).Concat(new[]{"--scene",configuration.Scene,"--output",configuration.OutputDirectory});
            if(request.ProtocolVersion==5)
            {
                arguments=arguments.Concat(new[]{"--room-scene",configuration.RoomScene!,"--room-resource-root",Path.GetDirectoryName(configuration.RoomScene!)!});
                if(request.Mode==RenderMode.DistributedAllCore)arguments=arguments.Concat(new[]{"--room-agents",_activeRoomAgentsFile!});
            }
            var child=new OwnedPythonProcess(configuration.Executable,arguments,AppContext.BaseDirectory);
            lock(_gate) { _child=child;LauncherPid=child.LauncherId; }
            stderr=DrainDiagnosticsAsync(child.Error);
            await SendAsync(new {command="hello",protocolVersion=5},linked.Token).ConfigureAwait(false);
            var line=await ReadFrameAsync(child.Output,linked.Token).ConfigureAwait(false) ?? throw new IOException("Python exited before readiness.");
            using(var ready=ParseStrict(line))
            {
                var value=ready.RootElement;
                var modes=value.GetProperty("modes").EnumerateArray().Select(x=>x.GetString()).ToArray();
                var required=request.Mode switch {RenderMode.DistributedAllCore=>"distributedAllCore",RenderMode.LocalAllCore=>"localAllCore",_=>"cpuSerialPreview"};
                var allowed=new[]{"cpuSerialPreview","localAllCore","distributedAllCore"};
                if(value.EnumerateObject().Count()!=5 || value.GetProperty("type").GetString()!="ready" || value.GetProperty("protocolVersion").GetInt32()!=request.ProtocolVersion
                    || value.GetProperty("sceneId").GetString()!=RenderProtocol.BenchmarkSceneId
                    || modes.Length!=modes.Distinct().Count() || !modes.Contains(required) || modes.Any(m=>!allowed.Contains(m))) throw new IOException("Incompatible Python readiness.");
                ComputationPid=value.GetProperty("pid").GetInt32();
                if(!child.Owns(ComputationPid.Value)) throw new IOException("Computation PID is outside the owned Job.");
                LastOwnedPids=child.ProcessIds();
                BackendStartupMilliseconds=startup.Elapsed.TotalMilliseconds;
                Trace.WriteLine($"DRT owned launcher={LauncherPid} computation={ComputationPid}; Job PIDs={string.Join(',',LastOwnedPids)}");
            }
            if(request.Pbr is not null)WritePbrEvidence(request,"request.json",ProtocolJson.SerializeRequest(request));
            await SendAsync(new {command="start",protocolVersion=request.ProtocolVersion,request=JsonSerializer.Deserialize<JsonElement>(ProtocolJson.SerializeRequest(request))},linked.Token).ConfigureAwait(false);
            bool cancel;lock(_gate) { _started=true;cancel=_cancel; }
            if(cancel) await SendAsync(new {command="cancel",protocolVersion=request.ProtocolVersion,runId=request.RunId},linked.Token).ConfigureAwait(false);
            var seconds=configuration.RoomRenderSeconds;
            if(seconds is <1 or >10840)throw new ArgumentException("Unsupported explicit room render deadline.");
            using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(seconds));
            using var render=CancellationTokenSource.CreateLinkedTokenSource(deadline.Token,_lifetime.Token);
            long sequence=0;
            var roomAcceptance=new RoomEventAcceptance(request.RunId,request.Width,request.Height,request.Mode);
            while(true)
            {
                line=await ReadFrameAsync(child.Output,render.Token).ConfigureAwait(false) ?? throw new IOException("Python EOF without terminal event.");
                using var strict=ParseStrict(line);
                var message=ProtocolJson.DeserializeEvent(line);
                if(message.ProtocolVersion!=request.ProtocolVersion || message.RunId!=request.RunId || message.Sequence!=sequence+1) throw new IOException("Invalid event RunId or sequence.");
                LastOwnedPids=LastOwnedPids.Union(child.ProcessIds()).ToArray();
                sequence=message.Sequence;
                if(roomAcceptance is not null)
                {
                    if(!roomAcceptance.Accept(message))throw new IOException("Invalid original-room stream/coverage/frame.");
                    ValidateRoomOwnership(message,request,child);
                    if(message is RoomEvent connectionEvent && request.Mode==RenderMode.DistributedAllCore)UpdateRoomRenderConnections(connectionEvent);
                    var evidencePath=Path.Combine(configuration.OutputDirectory,$"{request.RunId:D}.wpf.events.jsonl");
                    if(File.Exists(evidencePath) && new FileInfo(evidencePath).Length+line.Length+1>32*1024*1024)throw new IOException("Room evidence stream exceeds its bound.");
                    File.AppendAllText(evidencePath,line+"\n");
                    if(message is RoomEvent room && roomAcceptance.VerifiedRgb is {} rgb)
                        message=room with {PngBytes=RoomPng(request.Width,request.Height,rgb)};
                    if(RenderProtocol.IsTerminal(roomAcceptance.State))
                    {
                        await StopAsync().ConfigureAwait(false);
                        WritePbrEvidence(request,"lifecycle.json",JsonSerializer.Serialize(new{LauncherPid,ComputationPid,LastOwnedPids,TerminationVerified,ForcedTermination,ProcessExitCode,BackendStartupMilliseconds}));
                        if(ForcedTermination || ProcessExitCode!=0 || !TerminationVerified)throw new IOException("Room host cleanup was not natural and verified.");
                        await channel.Writer.WriteAsync(message,render.Token).ConfigureAwait(false);break;
                    }
                    await channel.Writer.WriteAsync(message,render.Token).ConfigureAwait(false);continue;
                }
            }
        }
        catch(Exception error) { failure=new IOException("Python PBR render failed: "+error.Message,error); }
        finally
        {
            try { await StopAsync().ConfigureAwait(false); }
            catch(Exception error) { failure=error; }
            if(failure is not null && ForcedTermination && TerminationVerified)
                failure=new IOException(failure.Message+" Forced stop verified for the owned process tree.",failure);
            if(stderr is not null) try { await stderr.WaitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false); } catch(Exception error) { failure ??=error; }
            channel.Writer.TryComplete(failure);
        }
    }

    public async Task RequestCancellationAsync(Guid runId,CancellationToken commandCancellation=default)
    {
        lock(_gate) { if(_run!=runId) return;_cancel=true; }
        // The start path sends pending intent after its start command, avoiding readiness races.
        bool started;lock(_gate) started=_started;
        if(started)
            try { await SendAsync(new {command="cancel",protocolVersion=_protocolVersion,runId},commandCancellation).ConfigureAwait(false); }
            catch(IOException) when(TerminationVerified) { }
        _=EnforceCancellationDeadlineAsync(runId,_lifetime.Token);
    }

    private async Task EnforceCancellationDeadlineAsync(Guid runId,CancellationToken token)
    {
        try
        {
            await Task.Delay(StopGraceMilliseconds,token).ConfigureAwait(false);
            lock(_gate) if(_run!=runId || TerminationVerified) return;
            await StopAsync().ConfigureAwait(false);
        }
        catch(OperationCanceledException) { }
        catch(Exception error) { Trace.TraceError("Cancellation cleanup failed: {0}",error); }
    }

    private async Task SendAsync(object message,CancellationToken token)
    {
        await _write.WaitAsync(token).ConfigureAwait(false);
        try { if(_child is { } child) await child.Input.WriteLineAsync(JsonSerializer.Serialize(message).AsMemory(),token).ConfigureAwait(false); }
        finally { _write.Release(); }
    }

    private Task StopAsync() { lock(_gate) return _child is null ? Task.CompletedTask : _stop ??= StopCoreAsync(); }
    private async Task StopCoreAsync()
    {
        await Task.Yield();
        OwnedPythonProcess? child;lock(_gate) child=_child;
        if(child is null) return;
        try
        {
            if(!child.HasExited)
            {
                using var timeout=new CancellationTokenSource(200);
                try { await SendAsync(new {command="shutdown",protocolVersion=_protocolVersion},timeout.Token).ConfigureAwait(false); } catch(Exception e) when(e is IOException or OperationCanceledException or ObjectDisposedException) { }
                try { await child.WaitForExitAsync(TimeSpan.FromMilliseconds(StopGraceMilliseconds)).ConfigureAwait(false); }
                catch(TimeoutException) { ForcedTermination=true; }
            }
            // Launcher exit can precede descendant/console teardown by a few milliseconds.
            var reap=Stopwatch.StartNew();
            while(child.HasExited && child.ProcessIds().Length!=0 && reap.ElapsedMilliseconds<500) await Task.Delay(10).ConfigureAwait(false);
            if(child.ProcessIds().Length!=0) { ForcedTermination=true; await child.TerminateAndVerifyAsync().ConfigureAwait(false); }
            TerminationVerified=child.ProcessIds().Length==0;
            if(!TerminationVerified) throw new IOException("Owned Python tree cleanup is unconfirmed.");
            await child.WaitForExitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false);
            ProcessExitCode=child.ExitCode;
            Trace.WriteLine($"DRT cleanup verified={TerminationVerified}, forced={ForcedTermination}, launcher={LauncherPid}, computation={ComputationPid}");
        }
        finally { child.Dispose();lock(_gate) _child=null; }
    }

    private async Task DrainDiagnosticsAsync(StreamReader reader)
    {
        var buffer=new char[1024];var retained=new StringBuilder();int count;
        try { while((count=await reader.ReadAsync(buffer).ConfigureAwait(false))!=0) if(retained.Length<16384) retained.Append(buffer,0,Math.Min(count,16384-retained.Length)); }
        catch(Exception error) when(error is ObjectDisposedException or IOException) { }
        finally { Diagnostics=retained.ToString(); }
    }

    internal static JsonDocument ParseStrict(string text)
    {
        var document=JsonDocument.Parse(text,new JsonDocumentOptions { MaxDepth=16 });
        void Check(JsonElement item)
        {
            if(item.ValueKind==JsonValueKind.Object) { var names=new HashSet<string>();foreach(var p in item.EnumerateObject()) { if(!names.Add(p.Name)) throw new JsonException("Duplicate property.");Check(p.Value); } }
            else if(item.ValueKind==JsonValueKind.Array) foreach(var child in item.EnumerateArray()) Check(child);
        }
        try { Check(document.RootElement);return document; } catch { document.Dispose();throw; }
    }

    private static async Task<string?> ReadFrameAsync(StreamReader reader,CancellationToken token)
    {
        var text=new StringBuilder();var character=new char[1];
        while(await reader.ReadAsync(character.AsMemory(),token).ConfigureAwait(false)!=0)
        {
            if(character[0]=='\n') return text.ToString();
            if(character[0]>127 || text.Length>=262144) throw new IOException("Malformed or oversized IPC frame.");
            text.Append(character[0]);
        }
        if(text.Length!=0) throw new IOException("Truncated IPC frame.");
        return null;
    }

    public async ValueTask DisposeAsync()
    {
        _disposed=true;_lifetime.Cancel();await Task.WhenAll(StopAsync(),DisconnectRoomAgentsAsync()).ConfigureAwait(false);
        if(_pump is not null) await _pump.WaitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
    }
}
