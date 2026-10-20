using System.Text.Json.Serialization;

namespace DistributedRayTrace.Desktop.Models;

public sealed record ApplicationSettings
{
    public int SchemaVersion { get; init; } = 2;
    public int Width { get; init; } = 160;
    public int Height { get; init; } = 90;
    [JsonIgnore]
    public bool IsValid => SchemaVersion == 2 && ResolutionPreset.Find(Width, Height) is not null;

    [JsonIgnore]
    public string ResolutionText => $"{Width} × {Height}";

    public static ApplicationSettings Default { get; } = new();
}
