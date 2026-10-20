using System.Globalization;
using System.Text.RegularExpressions;
using DistributedRayTrace.Contracts.Enums;

namespace DistributedRayTrace.Contracts.Models;

/// <summary>Canonical software Worker identity, independent of its process ID or CPU placement.</summary>
public readonly record struct RoomWorkerIdentity(string? NodeId,int Epoch,int Slot)
{
    public static bool TryParse(RenderMode mode,string? value,out RoomWorkerIdentity identity)
    {
        identity=default;
        if(value is null || value.Length>128)return false;
        if(mode==RenderMode.CpuSerialPreview)
            return value=="serial/worker-0";
        string? node=null;var epoch=0;var worker=value;
        if(mode==RenderMode.DistributedAllCore)
        {
            var parts=value.Split('/');
            if(parts.Length!=3 || !Regex.IsMatch(parts[0],@"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\z")
                || !parts[1].StartsWith('e') || !int.TryParse(parts[1].AsSpan(1),NumberStyles.None,CultureInfo.InvariantCulture,out epoch)
                || epoch is <1 or >8 || parts[1]!="e"+epoch)return false;
            node=parts[0];worker=parts[2];
        }
        else if(mode!=RenderMode.LocalAllCore)return false;
        if(!worker.StartsWith("worker-",StringComparison.Ordinal)
            || !int.TryParse(worker.AsSpan(7),NumberStyles.None,CultureInfo.InvariantCulture,out var slot)
            || slot<0 || slot>=RenderProtocol.MaxLocalWorkers || worker!="worker-"+slot)return false;
        identity=new(node,epoch,slot);return true;
    }
}
