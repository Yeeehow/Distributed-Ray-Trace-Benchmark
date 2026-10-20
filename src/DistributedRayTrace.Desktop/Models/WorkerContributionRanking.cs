using System.Globalization;
using DistributedRayTrace.Contracts;
using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Contracts.Models;

namespace DistributedRayTrace.Desktop.Models;

public sealed record WorkerContributionRow(string StableId,string DisplayName,long AcceptedRays,double SharePercent)
{
    public string CountText=>AcceptedRays.ToString(CultureInfo.InvariantCulture);
}
public sealed record WorkerContributionRanking(IReadOnlyList<WorkerContributionRow> Rows,long TotalRays)
{
    public static WorkerContributionRanking Create(RenderMode mode,IReadOnlyList<PbrContribution> contributors,long? expectedTotal=null,
        IReadOnlyList<RoomNode>? nodes=null)
    {
        ArgumentNullException.ThrowIfNull(contributors);
        if(mode is not (RenderMode.CpuSerialPreview or RenderMode.LocalAllCore or RenderMode.DistributedAllCore)
            || contributors.Count>RenderProtocol.MaxContributors)throw new ArgumentException("Unsupported contribution mode/capacity.");
        if(contributors.Select(c=>c.Id).Distinct(StringComparer.Ordinal).Count()!=contributors.Count)throw new ArgumentException("Duplicate accepted Worker.");
        var workers=contributors.Select(c=>
        {
            if(!RoomWorkerIdentity.TryParse(mode,c.Id,out var id) || c.Pid<=0 || c.AcceptedSamples<0
                || c.PrimaryRays!=c.AcceptedSamples || c.SecondaryRays<0 || c.SecondaryRays>c.PrimaryRays
                || c.ShadowRays<0 || c.ShadowRays>checked(5*c.PrimaryRays))throw new ArgumentException("Invalid accepted Worker.");
            return (Contribution:c,Identity:id);
        }).ToArray();
        if(mode!=RenderMode.DistributedAllCore && workers.Length>(mode==RenderMode.LocalAllCore?RenderProtocol.MaxLocalWorkers:1))
            throw new ArgumentException("Local Worker capacity exceeded.");
        var total=workers.Sum(c=>c.Contribution.TotalRays);
        if(expectedTotal is {} expected && total!=expected)throw new ArgumentException("Accepted Worker totals do not match the global counter.");
        WorkerContributionRow[] rows;
        if(mode==RenderMode.DistributedAllCore)
        {
            // Raw acceptance stays per Worker. Only the display groups epochs/Workers by machine.
            var counts=workers.GroupBy(w=>w.Identity.NodeId!,StringComparer.Ordinal)
                .ToDictionary(g=>g.Key,g=>g.Sum(w=>w.Contribution.TotalRays),StringComparer.Ordinal);
            if(nodes is not null)
            {
                if(nodes.Select(n=>n.Id).Distinct(StringComparer.Ordinal).Count()!=nodes.Count
                    || nodes.Any(n=>!RoomWorkerIdentity.TryParse(mode,n.Id+"/e1/worker-0",out _))
                    || counts.Keys.Any(id=>!nodes.Any(n=>n.Id==id)))throw new ArgumentException("Contribution Node differs from the run roster.");
                foreach(var node in nodes)counts.TryAdd(node.Id,0);
            }
            if(counts.Count>RenderProtocol.MaxRoomNodes || counts.Keys.Count(k=>k!=RenderProtocol.LocalNodeId)>RenderProtocol.MaxRoomAgents)
                throw new ArgumentException("Node display capacity exceeded.");
            rows=counts.OrderByDescending(p=>p.Value).ThenBy(p=>p.Key,StringComparer.Ordinal)
                .Select(p=>new WorkerContributionRow(p.Key,p.Key==RenderProtocol.LocalNodeId?"Local":p.Key,p.Value,total==0?0:100d*p.Value/total)).ToArray();
        }
        else
            rows=workers.OrderByDescending(w=>w.Contribution.TotalRays).ThenBy(w=>w.Identity.Slot)
                .Select(w=>new WorkerContributionRow(w.Contribution.Id,
                    (mode==RenderMode.CpuSerialPreview?"Serial":"Local")+" · Worker "+(w.Identity.Slot+1).ToString(CultureInfo.InvariantCulture),
                    w.Contribution.TotalRays,total==0?0:100d*w.Contribution.TotalRays/total)).ToArray();
        return new(Array.AsReadOnly(rows),total);
    }
}
