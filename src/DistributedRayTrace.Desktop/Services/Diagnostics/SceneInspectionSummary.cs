using System.Text.Json;

namespace DistributedRayTrace.Desktop.Services.Diagnostics;

public sealed class SceneInspectionException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}

public sealed record SceneInspectionIssue(string Code, string Severity, string Message, bool BlocksM3);

// Independent diagnostic DTO; never passed through RenderRequest/RenderEvent/ProtocolJson.
public sealed record SceneInspectionSummary(int SceneIndex, long Nodes, long MeshInstances, long Triangles,
    long Cameras, long Lights, long Materials, long Textures, bool M3InputReady,
    IReadOnlyList<SceneInspectionIssue> Issues, JsonElement Data)
{
    public static SceneInspectionSummary ParseResponse(string output, int exitCode)
    {
        try
        {
            using var document = JsonDocument.Parse(output, new JsonDocumentOptions { MaxDepth = 16 });
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object) throw Invalid();
            var ok = Bool(root.GetProperty("ok"));
            Object(root, "schema_version", "kind", "ok", ok ? "inspection" : "error");
            Identity(root, "drt.scene_inspection_response");
            if (!ok)
            {
                var error = root.GetProperty("error");
                Object(error, "code", "message");
                Text(error.GetProperty("message"), 256);
                var code = Text(error.GetProperty("code"), 64);
                var message = code switch
                {
                    "SCENE_FILE_ERROR" => "The scene file could not be read.",
                    "SCENE_UNSUPPORTED" => "The scene uses an unsupported format or feature.",
                    "SCENE_INPUT_ERROR" => "The scene contains invalid input data.",
                    "INTERNAL_ERROR" => "Scene inspection failed internally.",
                    _ => throw Invalid()
                };
                if (exitCode != (code == "INTERNAL_ERROR" ? 4 : 3)) throw Invalid();
                throw new SceneInspectionException(code, message);
            }
            if (exitCode != 0) throw Invalid();
            var value = root.GetProperty("inspection");
            Object(value, "schema_version", "kind", "scene_index", "root_node_indices", "counts", "camera_node_indices",
                "camera_selection_required", "light_node_indices", "bounds", "issues", "m3_input_ready", "readiness_scope");
            Identity(value, "drt.scene_inspection");
            if (Text(value.GetProperty("readiness_scope"), 64) != "static_inputs_only") throw Invalid();
            var sceneIndex = Index(value.GetProperty("scene_index"));
            Indices(value.GetProperty("root_node_indices"));
            Indices(value.GetProperty("camera_node_indices"));
            Bool(value.GetProperty("camera_selection_required"));
            var lightNodes = value.GetProperty("light_node_indices");
            Object(lightNodes, "point", "spot", "directional");
            foreach (var field in lightNodes.EnumerateObject()) Indices(field.Value);
            var counts = value.GetProperty("counts");
            Object(counts, "document", "active", "referenced");
            var doc = counts.GetProperty("document");
            Object(doc, "scenes", "nodes", "meshes", "cameras", "lights", "materials", "textures", "images", "samplers");
            var active = counts.GetProperty("active");
            Object(active, "nodes", "root_nodes", "mesh_definitions", "mesh_instances", "primitives", "geometry_triangles",
                "instanced_triangles", "camera_instances", "light_instances", "point_lights", "spot_lights", "directional_lights");
            var referenced = counts.GetProperty("referenced");
            Object(referenced, "materials", "textures", "images", "samplers");
            foreach (var group in counts.EnumerateObject())
                foreach (var field in group.Value.EnumerateObject()) Count(field.Value);
            var bounds = value.GetProperty("bounds");
            Object(bounds, "empty", "minimum", "maximum");
            var empty = Bool(bounds.GetProperty("empty"));
            foreach (var key in new[] { "minimum", "maximum" })
            {
                var vector = bounds.GetProperty(key);
                if (empty) { if (vector.ValueKind != JsonValueKind.Null) throw Invalid(); }
                else
                {
                    if (vector.ValueKind != JsonValueKind.Array || vector.GetArrayLength() != 3) throw Invalid();
                    foreach (var component in vector.EnumerateArray())
                        if (!component.TryGetDouble(out var number) || !double.IsFinite(number)) throw Invalid();
                }
            }
            var issues = value.GetProperty("issues");
            if (issues.ValueKind != JsonValueKind.Array) throw Invalid();
            var parsedIssues = new List<SceneInspectionIssue>();
            foreach (var issue in issues.EnumerateArray())
            {
                Object(issue, "code", "severity", "message", "source", "index", "blocks_m3");
                var severity = Text(issue.GetProperty("severity"), 16);
                if (severity is not ("error" or "warning" or "info")) throw Invalid();
                var source = issue.GetProperty("source");
                if (source.ValueKind != JsonValueKind.Null && Text(source, 16) is not ("node" or "scene")) throw Invalid();
                if (issue.GetProperty("index").ValueKind != JsonValueKind.Null) Index(issue.GetProperty("index"));
                parsedIssues.Add(new(Text(issue.GetProperty("code"), 64), severity,
                    Text(issue.GetProperty("message"), 512), Bool(issue.GetProperty("blocks_m3"))));
            }
            var ready = Bool(value.GetProperty("m3_input_ready"));
            if (ready == parsedIssues.Any(issue => issue.BlocksM3)) throw Invalid();
            return new(sceneIndex, Count(active.GetProperty("nodes")), Count(active.GetProperty("mesh_instances")),
                Count(active.GetProperty("instanced_triangles")), Count(active.GetProperty("camera_instances")),
                Count(active.GetProperty("light_instances")), Count(doc.GetProperty("materials")), Count(doc.GetProperty("textures")),
                ready, parsedIssues.AsReadOnly(), value.Clone());
        }
        catch (Exception error) when (error is JsonException or KeyNotFoundException or InvalidOperationException or FormatException or OverflowException)
        {
            throw Invalid();
        }
    }

    private static SceneInspectionException Invalid() => new("INVALID_RESPONSE", "Python returned an invalid diagnostic response.");
    private static void Object(JsonElement value, params string[] fields)
    {
        if (value.ValueKind != JsonValueKind.Object) throw Invalid();
        var actual = new HashSet<string>(StringComparer.Ordinal);
        foreach (var field in value.EnumerateObject()) if (!actual.Add(field.Name)) throw Invalid();
        if (!actual.SetEquals(fields)) throw Invalid();
    }
    private static void Identity(JsonElement value, string kind)
    {
        if (Count(value.GetProperty("schema_version")) != 1 || Text(value.GetProperty("kind"), 64) != kind) throw Invalid();
    }
    private static bool Bool(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.True => true, JsonValueKind.False => false, _ => throw Invalid()
    };
    private static long Count(JsonElement value) => value.TryGetInt64(out var count) && count >= 0 ? count : throw Invalid();
    private static int Index(JsonElement value) => value.TryGetInt32(out var index) && index >= 0 ? index : throw Invalid();
    private static void Indices(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Array) throw Invalid();
        foreach (var item in value.EnumerateArray()) Index(item);
    }
    private static string Text(JsonElement value, int limit)
    {
        if (value.ValueKind != JsonValueKind.String || value.GetString() is not { Length: > 0 } text
            || text.Length > limit || text.Any(char.IsControl)) throw Invalid();
        return text;
    }
}
