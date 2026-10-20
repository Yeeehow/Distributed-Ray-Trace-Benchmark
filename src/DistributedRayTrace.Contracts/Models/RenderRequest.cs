using DistributedRayTrace.Contracts.Enums;
using System.Text.Json.Serialization;

namespace DistributedRayTrace.Contracts.Models;

public sealed record RenderRequest
{
    public int ProtocolVersion { get; }
    public Guid RunId { get; }
    public string SceneId { get; }
    public RenderMode Mode { get; }
    public int Width { get; }
    public int Height { get; }
    public string? SceneVersion { get; }
    [JsonIgnore(Condition=JsonIgnoreCondition.WhenWritingNull)]
    public PbrConfiguration? Pbr { get; }

    public RenderRequest(int protocolVersion, Guid runId, string sceneId, RenderMode mode,
        int width, int height, string? sceneVersion = null, PbrConfiguration? pbr = null)
    {
        ContractGuard.Identity(protocolVersion, runId);
        ContractGuard.Dimensions(width, height);
        if(protocolVersion==RenderProtocol.RoomPbrVersion)
        {
            ContractGuard.Require(pbr==PbrConfiguration.Room(width,height) && sceneId==RenderProtocol.BenchmarkSceneId && sceneVersion is null
                && mode is RenderMode.CpuSerialPreview or RenderMode.LocalAllCore or RenderMode.DistributedAllCore,
                "Unsupported original-room PBR configuration.");
            ProtocolVersion=protocolVersion;RunId=runId;SceneId=sceneId;Mode=mode;Width=width;Height=height;SceneVersion=null;Pbr=pbr;return;
        }
        throw new ArgumentException("Only original-room PBR frontend v5 is active.");
    }
}
