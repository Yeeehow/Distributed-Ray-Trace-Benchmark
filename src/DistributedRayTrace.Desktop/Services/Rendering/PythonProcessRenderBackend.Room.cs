using System.IO;
using System.Text.Json;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Contracts.Models;
using DistributedRayTrace.Contracts;

namespace DistributedRayTrace.Desktop.Services.Rendering;

public sealed partial class PythonProcessRenderBackend
{
    public bool SupportsRoom=>configuration.RoomScene is not null && Path.IsPathFullyQualified(configuration.RoomScene);
    public IReadOnlyList<VerifiedRemoteNode> RoomRemoteNodes {get;private set;}=[];
    public bool SupportsRoomDistributed
    {
        get {var selected=RemotePeers.Where(p=>p.Selected).ToArray();return SupportsRoom && selected.Length>0 && selected.All(p=>p.Connected && p.Authenticated && p.Compatible);}
    }
    public string RoomAgentStatus {get;private set;}="Select trusted room PBR Agents in Remote.";
    private void ValidateRoomStart(RenderRequest r)
    {
        if(!SupportsRoom || r.ProtocolVersion!=5 || r.Pbr!=PbrConfiguration.Room(r.Width,r.Height)
            || r.Mode==RenderMode.DistributedAllCore && !SupportsRoomDistributed)
            throw new ArgumentException("Original-room PBR requires its provisioned source and selected compatible authenticated Agents.");
    }
    private async Task ProbeRoomAgentsAsync(CancellationToken token)
    {
        if(configuration.RoomAgentsFile is null || configuration.RoomScene is null || _pairedTopology is not null)return;
        try
        {
            await ImportRoomAgentsAsync(configuration.RoomAgentsFile,token).ConfigureAwait(false);
        }
        catch(Exception e) {RoomAgentStatus="Room Agent verification failed: "+e.Message;}
    }
    private void ValidateRoomOwnership(RenderEvent message,RenderRequest request,OwnedPythonProcess child)
    {
        if(message is not RoomEvent e)return;
        if(e.Contributions.Any(c=>!RoomWorkerIdentity.TryParse(request.Mode,c.Id,out _)))
            throw new IOException("Invalid room Worker identity.");
        if(request.Mode==RenderMode.CpuSerialPreview && e.Contributions.Any(c=>c.Id!="serial/worker-0" || c.Pid!=ComputationPid))
            throw new IOException("Serial room Worker ownership differs.");
        if(request.Mode==RenderMode.LocalAllCore && e.Contributions.Any(c=>!LastOwnedPids.Contains(c.Pid)))
            throw new IOException("Unowned room Local Worker.");
        if(request.Mode==RenderMode.DistributedAllCore && (e.Nodes.Any(n=>n.Id!=RenderProtocol.LocalNodeId && !_activeRoomNodes.Contains(n.Id,StringComparer.Ordinal))
            || e.Contributions.Any(c=>c.Id.StartsWith(RenderProtocol.LocalNodeId+"/",StringComparison.Ordinal)
                ? !LastOwnedPids.Contains(c.Pid) : !_activeRoomNodes.Any(a=>c.Id.StartsWith(a+"/",StringComparison.Ordinal)))))
            throw new IOException("Unverified room Remote Worker.");
    }
    private void WritePbrEvidence(RenderRequest request,string suffix,string text)
    {
        Directory.CreateDirectory(configuration.OutputDirectory);
        using var file=new FileStream(Path.Combine(configuration.OutputDirectory,$"{request.RunId:D}.wpf.{suffix}"),FileMode.CreateNew);
        using var writer=new StreamWriter(file);writer.Write(text);
    }
    private static byte[] RoomPng(int width,int height,byte[] rgb)
    {
        var bitmap=BitmapSource.Create(width,height,96,96,PixelFormats.Rgb24,null,rgb,width*3);bitmap.Freeze();
        var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(bitmap));
        using var stream=new MemoryStream();encoder.Save(stream);return stream.ToArray();
    }
}
