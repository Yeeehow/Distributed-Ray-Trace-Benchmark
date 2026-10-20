namespace DistributedRayTrace.Desktop.Models;

public sealed record ResolutionPreset(string Name, int Width, int Height)
{
    public string DisplayName => $"{Name} — {Width} × {Height}";

    public override string ToString() => DisplayName;

    public static IReadOnlyList<ResolutionPreset> All { get; } = Array.AsReadOnly(new[]
    {
        new ResolutionPreset("Compact", 160, 90),
        new ResolutionPreset("Detailed", 640, 360)
    });

    public static ResolutionPreset? Find(int width, int height) =>
        All.FirstOrDefault(preset => preset.Width == width && preset.Height == height);
}
