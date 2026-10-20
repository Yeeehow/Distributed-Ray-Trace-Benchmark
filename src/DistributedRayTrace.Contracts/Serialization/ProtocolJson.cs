using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.Json.Nodes;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Contracts.Models;

namespace DistributedRayTrace.Contracts.Serialization;

public static class ProtocolJson
{
    private static readonly JsonSerializerOptions Options = CreateOptions();

    private static JsonSerializerOptions CreateOptions()
    {
        var options = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
            MaxDepth = 16
        };
        options.Converters.Add(new JsonStringEnumConverter(JsonNamingPolicy.CamelCase, allowIntegerValues: false));
        return options;
    }

    public static string SerializeRequest(RenderRequest request) => JsonSerializer.Serialize(request, Options);
    public static RenderRequest DeserializeRequest(string json)
    {
        using var document=JsonDocument.Parse(json);var root=document.RootElement;
        if(root.GetProperty("protocolVersion").GetInt32()!=5)throw new JsonException("Original-room v5 request required.");
        Exact(root,"protocolVersion runId sceneId mode width height sceneVersion pbr");PbrFields(root.GetProperty("pbr"));
        return JsonSerializer.Deserialize<RenderRequest>(json, Options)??throw new JsonException("Null request.");
    }
    private static void Exact(JsonElement value,string fields)
    {
        if(value.ValueKind!=JsonValueKind.Object || !value.EnumerateObject().Select(p=>p.Name).Order().SequenceEqual(fields.Split(' ').Order()))
            throw new JsonException("Exact PBR field set required.");
    }
    private static void PbrFields(JsonElement v)=>Exact(v,"workloadKind presetId resourceSha256 workloadDigest targetSpp seed maxDepth outputPolicy");
    public static string SerializeEvent(RenderEvent value)
    {
        return JsonSerializer.Serialize(value,Options);
    }
    public static RenderEvent DeserializeEvent(string json)
    {
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        if(root.GetProperty("protocolVersion").GetInt32()==5)
        {
            var type=root.GetProperty("type").GetString();
            if(type=="roomFrame")Exact(root,"type protocolVersion runId sequence transfer");
            else if(type=="roomEvent")
            {
                Exact(root,"type protocolVersion runId sequence schemaVersion configuration phase width height acceptedSamples totalSamples frameId counters contributions nodes linearSha256 rgb8Sha256 elapsedMilliseconds renderNanoseconds error coverageComplete isBenchmarkValid");
                PbrFields(root.GetProperty("configuration"));
                var counters=root.GetProperty("counters");
                if(counters.ValueKind!=JsonValueKind.Null)Exact(counters,"primaryRays secondaryRays shadowRays pathCount surfaceInteractions schemaVersion");
                foreach(var c in root.GetProperty("contributions").EnumerateArray())Exact(c,"id pid acceptedSamples primaryRays secondaryRays shadowRays");
                foreach(var n in root.GetProperty("nodes").EnumerateArray())Exact(n,"id state session generation admitted active capabilityConfirmed authenticated");
            }
            else throw new JsonException("Explicit room v5 event required.");
        }
        if(root.GetProperty("protocolVersion").GetInt32()!=5)throw new JsonException("Original-room v5 event required.");
        return JsonSerializer.Deserialize<RenderEvent>(json, Options) ?? throw new JsonException("Null event.");
    }
}
