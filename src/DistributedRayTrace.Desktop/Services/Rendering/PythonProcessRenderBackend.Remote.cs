using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using DistributedRayTrace.Contracts;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.Services.Rendering;

public sealed partial class PythonProcessRenderBackend
{
    private IReadOnlyList<string> RoomRemoteArguments(params string[] arguments)=>
        (configuration.HostPrefix is null
            ? new[]{"-I","-B","-X","utf8","-u","-m","drt_backend.room_pbr.remote"}
            : new[]{"--room-remote"}).Concat(arguments).ToArray();
    private readonly object _remoteGate=new();
    private readonly SemaphoreSlim _remoteOperations=new(1,1);
    private JsonNode? _pairedTopology;
    private Dictionary<string,RemotePeerState> _peers=new(StringComparer.Ordinal);
    private readonly Dictionary<string,RoomNode> _renderConnections=new(StringComparer.Ordinal);
    private OwnedPythonProcess? _monitor;
    private Task? _monitorReader;
    private Guid _monitorGeneration;
    private string[] _monitorExpected=[];
    private string[] _activeRoomNodes=[];
    private string? _activeRoomAgentsFile;
    private bool _maintainRoomConnections;
    public event EventHandler? RemoteChanged;
    public IReadOnlyList<RemotePeerState> RemotePeers
    {
        get {lock(_remoteGate)return _peers.Values.OrderBy(p=>p.Id,StringComparer.Ordinal).Select(p=>
            _renderConnections.TryGetValue(p.Id,out var live) && live.Authenticated && live.Session is not null
                ?p with {Connected=true,Authenticated=true,Compatible=true,Session=live.Session,State="Connected · rendering"}:p).ToArray();}
    }
    public void SelectRoomPeer(string id,bool selected)
    {
        lock(_gate)if(_run is not null)throw new InvalidOperationException("Selection is locked during a render.");
        lock(_remoteGate)
        {
            if(!_peers.TryGetValue(id,out var p))throw new ArgumentException("Unknown unpaired Agent.");
            _peers[id]=p with {Selected=selected};
        }
        RemoteChanged?.Invoke(this,EventArgs.Empty);
    }
    private void UpdateRoomRenderConnections(RoomEvent e)
    {
        lock(_remoteGate)
        {
            foreach(var node in e.Nodes)_renderConnections[node.Id]=node;
        }
        RemoteChanged?.Invoke(this,EventArgs.Empty);
    }
    public async Task ImportRoomAgentsAsync(string path,CancellationToken token=default)
    {
        lock(_gate)if(_run is not null)throw new InvalidOperationException("Pairing is locked during a render.");
        if(!Path.IsPathFullyQualified(path) || new FileInfo(path).Length>32768)throw new ArgumentException("Choose a bounded local trusted topology.");
        await DisconnectRoomAgentsAsync().ConfigureAwait(false);
        var raw=await File.ReadAllBytesAsync(path,token).ConfigureAwait(false);
        using var child=new OwnedPythonProcess(configuration.Executable,RoomRemoteArguments("--list","--agents",path),AppContext.BaseDirectory);
        try
        {
            using var deadline=CancellationTokenSource.CreateLinkedTokenSource(token);deadline.CancelAfter(TimeSpan.FromSeconds(10));
            var diagnostics=DrainProbeErrorAsync(child.Error,deadline.Token);
            var line=await ReadFrameAsync(child.Output,deadline.Token).ConfigureAwait(false)??throw new IOException("Paired topology EOF.");
            await child.WaitForExitAsync(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
            using var doc=ParseStrict(line);var r=doc.RootElement;
            if(r.EnumerateObject().Count()!=3 || r.GetProperty("schema").GetString()!="drt.room_paired_topology.v1" || child.ExitCode!=0
                || r.GetProperty("sha256").GetString()!=Convert.ToHexString(SHA256.HashData(raw)).ToLowerInvariant())throw new IOException("Paired topology changed or is unsupported.");
            var peers=new Dictionary<string,RemotePeerState>(StringComparer.Ordinal);
            foreach(var n in r.GetProperty("nodes").EnumerateArray())
            {
                if(n.EnumerateObject().Count()!=4)throw new IOException("Paired Node metadata differs.");
                var id=n.GetProperty("id").GetString()!;
                if(id==RenderProtocol.LocalNodeId)throw new IOException("drt-local is reserved for the owned local participant.");
                peers.Add(id,new(id,n.GetProperty("host").GetString()!,n.GetProperty("port").GetInt32(),n.GetProperty("fingerprint").GetString()!,
                    false,false,false,false,"Paired · disconnected",null,0,0));
            }
            if(peers.Count is <1 or >RenderProtocol.MaxRoomAgents)throw new IOException("Paired Agent bound.");
            using var original=ParseStrict(System.Text.Encoding.UTF8.GetString(raw));
            lock(_remoteGate) {_pairedTopology=JsonNode.Parse(original.RootElement.GetRawText());_peers=peers;_renderConnections.Clear();}
            RoomRemoteNodes=[];RoomAgentStatus=$"{peers.Count} pinned Agents paired. Select Agents and connect in Remote.";
            await diagnostics.ConfigureAwait(false);RemoteChanged?.Invoke(this,EventArgs.Empty);
        }
        catch {await child.TerminateAndVerifyAsync().ConfigureAwait(false);throw;}
    }
    private string SelectedRoomTopology(Guid run,bool requireConnected)
    {
        JsonNode copy;string[] ids;
        lock(_remoteGate)
        {
            var selected=_peers.Values.Where(p=>p.Selected).ToArray();
            if(requireConnected && selected.Any(p=>!p.Connected || !p.Authenticated || !p.Compatible))throw new InvalidOperationException("Every selected Agent must be authenticated and compatible.");
            ids=selected.Select(p=>p.Id).ToArray();
            if(ids.Length==0 || _pairedTopology is null)throw new InvalidOperationException("Select and authenticate compatible paired Agents.");
            copy=_pairedTopology.DeepClone();
            copy["agents"]=new JsonArray(copy["agents"]!.AsArray().Where(a=>ids.Contains(a!["node_id"]!.GetValue<string>(),StringComparer.Ordinal)).Select(a=>a!.DeepClone()).ToArray());
            // Preserve an explicit administrative ceiling; omitted/auto uses the global safety cap.
            copy["global_workers"]??=JsonValue.Create("auto");
            if(requireConnected)_activeRoomNodes=ids;
        }
        Directory.CreateDirectory(configuration.OutputDirectory);
        if((File.GetAttributes(configuration.OutputDirectory)&FileAttributes.ReparsePoint)!=0)throw new IOException("Reparse topology output directory.");
        var file=Path.Combine(configuration.OutputDirectory,$"{run:D}.selected-agents.json");
        using var stream=new FileStream(file,FileMode.CreateNew,FileAccess.Write,FileShare.None);
        using var writer=new StreamWriter(stream);writer.Write(copy.ToJsonString());return file;
    }
    public async Task ConnectRoomAgentsAsync(int width,int height,CancellationToken token=default)
    {
        lock(_gate)if(_run is not null)throw new InvalidOperationException("Connection changes are locked during a render.");
        await _remoteOperations.WaitAsync(token).ConfigureAwait(false);
        try
        {
            await StopRoomMonitorCoreAsync().ConfigureAwait(false);var generation=Guid.NewGuid();var file=SelectedRoomTopology(generation,false);
            lock(_remoteGate) {_monitorGeneration=generation;_monitorExpected=_peers.Values.Where(p=>p.Selected).Select(p=>p.Id).ToArray();}
            var child=new OwnedPythonProcess(configuration.Executable,RoomRemoteArguments("--monitor","--agents",file,"--scene",configuration.RoomScene!,"--width",width.ToString(),"--height",height.ToString(),
                "--generation",generation.ToString("D"),"--lifetime-seconds","21600"),AppContext.BaseDirectory);
            _monitor=child;var ready=new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            _monitorReader=ReadRoomMonitorAsync(child,generation,ready);
            try {await ready.Task.WaitAsync(TimeSpan.FromSeconds(15),token).ConfigureAwait(false);}
            catch {await StopRoomMonitorCoreAsync().ConfigureAwait(false);throw;}
            _maintainRoomConnections=true;
        }
        finally {_remoteOperations.Release();}
    }
    private async Task ReadRoomMonitorAsync(OwnedPythonProcess child,Guid generation,TaskCompletionSource ready)
    {
        long sequence=0;var error=DrainProbeErrorAsync(child.Error,CancellationToken.None);
        try
        {
            while(await ReadFrameAsync(child.Output,CancellationToken.None).ConfigureAwait(false) is {} line)
            {
                using var doc=ParseStrict(line);var r=doc.RootElement;
                if(r.EnumerateObject().Count()!=5 || r.GetProperty("type").GetString()!="remoteState" || r.GetProperty("schema").GetString()!="drt.room_monitor_frontend.v1"
                    || r.GetProperty("generation").GetString()!=generation.ToString("D") || r.GetProperty("sequence").GetInt64()!=++sequence)throw new IOException("Foreign monitor event.");
                var states=r.GetProperty("nodes").EnumerateArray().ToArray();
                if(states.Length>RenderProtocol.MaxRoomAgents || states.Select(n=>n.GetProperty("id").GetString()).Distinct().Count()!=states.Length)throw new IOException("Monitor Node bound.");
                lock(_remoteGate)
                {
                    if(_monitorGeneration!=generation)return;
                    foreach(var n in states)
                    {
                        var id=n.GetProperty("id").GetString()!;
                        if(n.EnumerateObject().Count()!=7 || !_monitorExpected.Contains(id,StringComparer.Ordinal) || !_peers.TryGetValue(id,out var prior))throw new IOException("Unselected monitor Node.");
                        var auth=n.GetProperty("authenticated").GetBoolean();var compatible=n.GetProperty("compatible").GetBoolean();
                        var state=n.GetProperty("state").GetString();var session=n.GetProperty("session").ValueKind==JsonValueKind.Null?null:n.GetProperty("session").GetString();
                        var workers=n.GetProperty("available_workers").GetInt32();var pid=n.GetProperty("agent_pid").GetInt32();
                        if(auth!=(state=="Connected") || state is not ("Connected" or "Disconnected") || auth && (!compatible || !Guid.TryParseExact(session,"D",out var sid) || sid==Guid.Empty || workers is <1 or >RenderProtocol.MaxLocalWorkers || pid<=0)
                            || !auth && (session is not null || workers!=0))throw new IOException("Invalid monitor connection state.");
                        _peers[id]=prior with {Connected=auth,Authenticated=auth,Compatible=compatible,Session=session,State=state!,AvailableWorkers=workers,AgentPid=pid};
                    }
                    RoomRemoteNodes=_peers.Values.Where(p=>p.Compatible).Select(p=>new VerifiedRemoteNode(p.Id,Math.Max(1,p.AvailableWorkers),p.AgentPid)).ToArray();
                    if(states.Length==_monitorExpected.Length)ready.TrySetResult();
                }
                RoomAgentStatus=RemotePeers.Where(p=>p.Selected).All(p=>p.Connected && p.Authenticated && p.Compatible)
                    ?"Selected remote connections are live. Distributed uses Local + remote CPUs; each run authenticates again."
                    :"Some selected remote connections are unavailable. Check the Agent and use Remote to reconnect.";
                RemoteChanged?.Invoke(this,EventArgs.Empty);
            }
        }
        catch(Exception e) {ready.TrySetException(e);RoomAgentStatus="Remote connection ended: "+e.Message;}
        finally
        {
            lock(_remoteGate)if(_monitorGeneration==generation)
                foreach(var id in _monitorExpected)if(_peers.TryGetValue(id,out var p))_peers[id]=p with {Connected=false,Authenticated=false,Session=null,AvailableWorkers=0,State="Disconnected"};
            ready.TrySetException(new IOException("Monitor stream ended."));RemoteChanged?.Invoke(this,EventArgs.Empty);
            RoomAgentStatus=_activeRoomAgentsFile is not null?"Distributed session uses Local and selected remote Agents.":
                "Idle connections closed. Select Agents and connect before Distributed execution.";
            try {await error.ConfigureAwait(false);}catch(Exception) { }
        }
    }
    public async Task DisconnectRoomAgentsAsync()
    {
        await _remoteOperations.WaitAsync().ConfigureAwait(false);
        try {_maintainRoomConnections=false;await StopRoomMonitorCoreAsync().ConfigureAwait(false);}finally {_remoteOperations.Release();}
    }
    private async Task SuspendRoomMonitorAsync()
    {
        await _remoteOperations.WaitAsync().ConfigureAwait(false);
        try {await StopRoomMonitorCoreAsync().ConfigureAwait(false);}finally {_remoteOperations.Release();}
    }
    private async Task RestoreRoomMonitorAsync(int width,int height)
    {
        if(_disposed || !_maintainRoomConnections)return;
        RoomAgentStatus="Restoring selected remote connections for the next run.";
        RemoteChanged?.Invoke(this,EventArgs.Empty);
        try
        {
            using var deadline=new CancellationTokenSource(TimeSpan.FromSeconds(18));
            await ConnectRoomAgentsAsync(width,height,deadline.Token).ConfigureAwait(false);
        }
        catch(Exception e)
        {
            RoomAgentStatus="Automatic reconnect failed: "+e.Message+" Use Remote to reconnect.";
            RemoteChanged?.Invoke(this,EventArgs.Empty);
        }
    }
    private async Task StopRoomMonitorCoreAsync()
    {
        var child=_monitor;_monitor=null;if(child is null)return;
        try
        {
            if(!child.HasExited) {await child.Input.WriteLineAsync("{\"command\":\"shutdown\"}").ConfigureAwait(false);await child.WaitForExitAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);}
            if(_monitorReader is not null)await _monitorReader.WaitAsync(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
            var reap=System.Diagnostics.Stopwatch.StartNew();
            while(child.ProcessIds().Length!=0 && reap.ElapsedMilliseconds<500)await Task.Delay(10).ConfigureAwait(false);
            if(child.ProcessIds().Length!=0)await child.TerminateAndVerifyAsync().ConfigureAwait(false);
        }
        catch(Exception) {await child.TerminateAndVerifyAsync().ConfigureAwait(false);}
        finally {child.Dispose();_monitorReader=null;}
    }
}
