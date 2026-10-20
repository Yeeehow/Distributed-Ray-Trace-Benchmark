namespace DistributedRayTrace.Desktop.Models;

public sealed record RemotePeerState(string Id,string Host,int Port,string Fingerprint,bool Selected,bool Connected,
    bool Authenticated,bool Compatible,string State,string? Session,int AvailableWorkers,int AgentPid);
