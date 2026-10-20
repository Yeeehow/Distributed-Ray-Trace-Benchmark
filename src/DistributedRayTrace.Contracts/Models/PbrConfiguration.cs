namespace DistributedRayTrace.Contracts.Models;

/// <summary>Original-room application presets; Python owns their complete policies.</summary>
public sealed record PbrConfiguration
{
    public string WorkloadKind {get;}
    public string PresetId {get;}
    public string ResourceSha256 {get;}
    public string WorkloadDigest {get;}
    public int TargetSpp {get;}
    public long Seed {get;}
    public int MaxDepth {get;}
    public string OutputPolicy {get;}
    public const string RoomPreset="original-room-camera5-v1";
    public const string RoomRaw="553b9bd35d9234a319b2d8424fb8c0e95a0af722327ddd6103485d7d809ff13b";
    public static string RoomDigest(int width,int height)=>(width,height) switch {
        (160,90)=>"4720b5e056efdbb3f40714f393fe8608ab042be5ca4216fbf075770777982260",
        (640,360)=>"cf602253176ae14e2091941ed2b2bc38070cb92d4bb45b32911fcc79b820d533",
        _=>throw new ArgumentException("Unsupported room resolution.")};
    public static PbrConfiguration Room(int width,int height)=>new("drt.pbr_pathtracer.v1",RoomPreset,RoomRaw,
        RoomDigest(width,height),1,19,1,"drt.color.room_srgb_exposure.v1");
    public PbrConfiguration(string workloadKind,string presetId,string resourceSha256,string workloadDigest,
        int targetSpp,long seed,int maxDepth,string outputPolicy)
    {
        var room=workloadKind=="drt.pbr_pathtracer.v1" && presetId==RoomPreset && resourceSha256==RoomRaw
            && (workloadDigest==RoomDigest(160,90) || workloadDigest==RoomDigest(640,360))
            && targetSpp==1 && seed==19 && maxDepth==1 && outputPolicy=="drt.color.room_srgb_exposure.v1";
        ContractGuard.Require(room,"Unsupported PBR preset/numerical identity.");
        WorkloadKind=workloadKind;PresetId=presetId;ResourceSha256=resourceSha256;WorkloadDigest=workloadDigest;
        TargetSpp=targetSpp;Seed=seed;MaxDepth=maxDepth;OutputPolicy=outputPolicy;
    }
}
