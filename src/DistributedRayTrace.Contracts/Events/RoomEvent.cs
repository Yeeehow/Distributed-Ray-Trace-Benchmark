using System.Text.Json;
using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Models;

namespace DistributedRayTrace.Contracts.Events;

public sealed record RoomNode(string Id,string State,string? Session,int Generation,int Admitted,int Active,
    bool CapabilityConfirmed,bool Authenticated);

public sealed record RoomFrameEvent : RenderEvent
{
    public JsonElement Transfer {get;}
    public RoomFrameEvent(int protocolVersion,Guid runId,long sequence,JsonElement transfer):base(protocolVersion,runId,sequence)
    {
        ContractGuard.Require(protocolVersion==5,"Room transfer requires v5.");Transfer=transfer.Clone();
    }
}

public sealed record RoomEvent : RenderEvent
{
    [System.Text.Json.Serialization.JsonIgnore]
    public byte[]? PngBytes {get;init;}
    public int SchemaVersion {get;}
    public PbrConfiguration Configuration {get;}
    public string Phase {get;}
    public int Width {get;}
    public int Height {get;}
    public long AcceptedSamples {get;}
    public long TotalSamples {get;}
    public string? FrameId {get;}
    public PbrCounters? Counters {get;}
    public IReadOnlyList<PbrContribution> Contributions {get;}
    public IReadOnlyList<RoomNode> Nodes {get;}
    public string? LinearSha256 {get;}
    public string? Rgb8Sha256 {get;}
    public long? ElapsedMilliseconds {get;}
    public long? RenderNanoseconds {get;}
    public RenderError? Error {get;}
    public bool CoverageComplete {get;}
    public bool IsBenchmarkValid {get;}
    public RoomEvent(int protocolVersion,Guid runId,long sequence,int schemaVersion,PbrConfiguration configuration,
        string phase,int width,int height,long acceptedSamples,long totalSamples,string? frameId,PbrCounters? counters,
        IReadOnlyList<PbrContribution> contributions,IReadOnlyList<RoomNode> nodes,string? linearSha256,string? rgb8Sha256,
        long? elapsedMilliseconds,long? renderNanoseconds,RenderError? error,bool coverageComplete,bool isBenchmarkValid):base(protocolVersion,runId,sequence)
    {
        ContractGuard.Require(protocolVersion==5 && schemaVersion==2 && configuration==PbrConfiguration.Room(width,height)
            && !isBenchmarkValid,"Explicit room configuration required.");
        ContractGuard.Require(phase is "preparing" or "rendering" or "progress" or "preview" or "frame" or "completed"
            or "cancelling" or "cancelled" or "failed" or "node","Unsupported room phase.");
        ContractGuard.Require(totalSamples==(long)width*height*configuration.TargetSpp && acceptedSamples>=0 && acceptedSamples<=totalSamples,
            "Invalid room sample coverage.");
        ContractGuard.Require(contributions is not null && contributions.Count<=RenderProtocol.MaxContributors
            && nodes is not null && nodes.Count<=RenderProtocol.MaxRoomNodes,"Room registry bound.");
        ContractGuard.Require((phase=="failed")== (error is not null) && elapsedMilliseconds is null or >=0
            && renderNanoseconds is null or >0,"Invalid room failure/timing.");
        ContractGuard.Require((frameId is not null)==(phase is "preview" or "frame")
            && (frameId is null || Guid.TryParseExact(frameId,"D",out var id) && id!=Guid.Empty),"Room frame reference differs.");
        SchemaVersion=schemaVersion;Configuration=configuration;Phase=phase;Width=width;Height=height;
        AcceptedSamples=acceptedSamples;TotalSamples=totalSamples;FrameId=frameId;Counters=counters;
        Contributions=contributions!.ToArray();Nodes=nodes!.ToArray();LinearSha256=linearSha256;Rgb8Sha256=rgb8Sha256;
        ElapsedMilliseconds=elapsedMilliseconds;RenderNanoseconds=renderNanoseconds;Error=error;CoverageComplete=coverageComplete;IsBenchmarkValid=false;
    }
}

/// <summary>Ordered room messages use the existing session lifecycle and central numerical ledger.</summary>
public sealed class RoomEventAcceptance(Guid run,int width,int height,RenderMode mode)
{
    private readonly RoomFrameAssembler _frames=new(run,PbrConfiguration.RoomDigest(width,height),width,height);
    private (string Id,string Purpose,byte[] Rgb)? _pending;
    private bool _rendering,_final;
    private string? _linear,_rgb;
    private PbrCounters? _finalCounters;
    private long? _finalNanoseconds;
    private IReadOnlyList<PbrContribution> _finalContributors=[];
    private long _preview;
    private IReadOnlyList<PbrContribution> _contributors=[];
    public long Sequence {get;private set;}
    public long Accepted {get;private set;}
    public RenderState State {get;private set;}=RenderState.Preparing;
    public byte[]? VerifiedRgb {get;private set;}
    public bool Accept(RenderEvent message)
    {
        if(RenderProtocol.IsTerminal(State) || message.ProtocolVersion!=5 || message.RunId!=run || message.Sequence!=Sequence+1 || message.Sequence>1024)return false;
        VerifiedRgb=null;
        if(message is RoomFrameEvent transfer)
        {
            if(!_rendering || _final || _pending is not null)return false;
            var result=_frames.Accept(5,run,transfer.Sequence,transfer.Transfer);
            if(result is not null)_pending=(transfer.Transfer.GetProperty("frameId").GetString()!,result.Value.Purpose,result.Value.Rgb);
            Sequence=message.Sequence;return true;
        }
        if(message is not RoomEvent e || _frames.IsActive || e.Width!=width || e.Height!=height
            || e.Configuration!=PbrConfiguration.Room(width,height) || e.AcceptedSamples<Accepted)return false;
        // Historical epoch rows may remain after shutdown; cap active ownership separately.
        if(mode!=RenderMode.DistributedAllCore && (e.Nodes.Count!=0
            || e.Contributions.Count>(mode==RenderMode.LocalAllCore?RenderProtocol.MaxLocalWorkers:1)))return false;
        var total=(long)width*height;
        if(e.Contributions.Select(c=>c.Id).Distinct(StringComparer.Ordinal).Count()!=e.Contributions.Count
            || e.Contributions.Any(c=>!RoomWorkerIdentity.TryParse(mode,c.Id,out _) || c.Pid<=0 || c.AcceptedSamples<0 || c.AcceptedSamples>total
                || c.PrimaryRays!=c.AcceptedSamples || c.SecondaryRays<0 || c.SecondaryRays>c.PrimaryRays
                || c.ShadowRays<0 || c.ShadowRays>5*c.PrimaryRays
                || _contributors.Any(old=>old.Id==c.Id && (old.Pid!=c.Pid || old.AcceptedSamples>c.AcceptedSamples
                    || old.SecondaryRays>c.SecondaryRays || old.ShadowRays>c.ShadowRays)))
            || _contributors.Any(old=>!e.Contributions.Any(c=>c.Id==old.Id))
            || e.Contributions.Sum(c=>c.AcceptedSamples)!=e.AcceptedSamples)return false;
        if(e.Nodes.Count(n=>n.Id!=RenderProtocol.LocalNodeId)>RenderProtocol.MaxRoomAgents
            || e.Nodes.Select(n=>n.Id).Distinct().Count()!=e.Nodes.Count || e.Nodes.Any(n=>string.IsNullOrWhiteSpace(n.Id) || n.Id.Length>64
            || n.Generation<0 || n.Generation>8 || n.Admitted<0 || n.Admitted>RenderProtocol.MaxLocalWorkers || n.Active<0 || n.Active>n.Admitted
            || n.State is not ("Configured" or "Authenticating" or "Preparing" or "Ready" or "Busy" or "Draining" or "Stopped" or "Lost" or "Unavailable" or "Suspect" or "Rejoining")
            || n.Session is not null && (!Guid.TryParseExact(n.Session,"D",out var id) || id==Guid.Empty)
            || n.Authenticated && (n.Session is null || n.State is not ("Preparing" or "Ready" or "Busy" or "Draining"))
            || n.CapabilityConfirmed!=n.Authenticated
            || n.Session is null && (n.Admitted!=0 || n.Active!=0)
            || n.State is "Stopped" or "Lost" or "Unavailable" && (n.Admitted!=0 || n.Active!=0))
            || e.Nodes.Sum(n=>n.Admitted)>RenderProtocol.MaxDistributedWorkers)return false;
        if(mode==RenderMode.DistributedAllCore && e.Contributions.Any(c=>
            !e.Nodes.Any(n=>n.Id==c.Id.Split('/')[0])))return false;
        if(e.Phase=="preparing" && (Sequence!=0 || e.AcceptedSamples!=0)
            || e.Phase=="rendering" && (_rendering || State!=RenderState.Preparing)
            || e.Phase is "progress" or "preview" or "frame" && !_rendering
            || _final && e.Phase is "progress" or "preview" or "cancelling" or "cancelled")return false;
        if(e.CoverageComplete!=(e.Phase is "frame" or "completed"))return false;
        if(e.Phase is "preview" or "frame")
        {
            if(_pending is not {} p || p.Id!=e.FrameId || p.Purpose!=(e.Phase=="preview"?"preview":"final")
                || e.AcceptedSamples==0 || e.Phase=="preview" && e.AcceptedSamples<=_preview)return false;
            if(e.Phase=="frame" && (_final || e.AcceptedSamples!=total || !Hash(e.LinearSha256) || !Hash(e.Rgb8Sha256)
                || Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(p.Rgb)).ToLowerInvariant()!=e.Rgb8Sha256
                || !CompleteData(e,total)))return false;
        }
        else if(_pending is not null)return false;
        if(e.Phase=="completed")
        {
            if(!_final || e.AcceptedSamples!=total || e.LinearSha256!=_linear || e.Rgb8Sha256!=_rgb
                || !CompleteData(e,total) || e.Counters!=_finalCounters || e.RenderNanoseconds!=_finalNanoseconds
                || !e.Contributions.SequenceEqual(_finalContributors))return false;
        }
        if(e.Phase=="cancelled" && State!=RenderState.Cancelling)return false;
        Sequence=e.Sequence;Accepted=e.AcceptedSamples;_contributors=e.Contributions;
        if(_pending is {} pending) {VerifiedRgb=pending.Rgb;_pending=null;}
        if(e.Phase=="preview")_preview=e.AcceptedSamples;
        if(e.Phase=="rendering") {_rendering=true;State=RenderState.Rendering;}
        if(e.Phase=="frame") {_final=true;_linear=e.LinearSha256;_rgb=e.Rgb8Sha256;
            _finalCounters=e.Counters;_finalNanoseconds=e.RenderNanoseconds;_finalContributors=e.Contributions;}
        if(e.Phase=="cancelling")State=RenderState.Cancelling;
        if(e.Phase=="cancelled")State=RenderState.Cancelled;
        if(e.Phase=="failed")State=RenderState.Failed;
        if(e.Phase=="completed")State=RenderState.Completed;
        return true;
    }
    private bool CompleteData(RoomEvent e,long total)
    {
        var c=e.Counters;
        return e.RenderNanoseconds is >0 && e.ElapsedMilliseconds==e.RenderNanoseconds/1_000_000
            && c is not null && c.SchemaVersion==1 && c.PrimaryRays==total && c.PathCount==total
            && c.SecondaryRays>=0 && c.SecondaryRays<=total && c.ShadowRays>=0 && c.ShadowRays<=5*total
            && c.SurfaceInteractions>=0 && c.SurfaceInteractions<=2*total
            && e.Contributions.Count>0 && e.Contributions.Sum(v=>v.AcceptedSamples)==total
            && e.Contributions.Sum(v=>v.PrimaryRays)==c.PrimaryRays && e.Contributions.Sum(v=>v.SecondaryRays)==c.SecondaryRays
            && e.Contributions.Sum(v=>v.ShadowRays)==c.ShadowRays
            && (mode!=RenderMode.DistributedAllCore || e.Nodes.Count>0 && e.Nodes.All(n=>!n.Authenticated && n.Admitted==0 && n.Active==0));
    }
    private static bool Hash(string? s)=>s is {Length:64} && s.All(c=>c is >= '0' and <= '9' or >= 'a' and <= 'f');
}
