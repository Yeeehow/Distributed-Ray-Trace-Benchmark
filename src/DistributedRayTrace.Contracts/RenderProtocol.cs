namespace DistributedRayTrace.Contracts;

public static class RenderProtocol
{
    public const int Version = 5;
    public const int RoomPbrVersion = 5;
    public const string BenchmarkSceneId = "shogi-match-room-v3";
    public const int MaxFrameBytes = 32 * 1024 * 1024;
    public const int MaxContributors = 4096;
    public const int MaxLocalWorkers = 64;
    public const int MaxDistributedWorkers = 256;
    public const int MaxRoomAgents = 16;
    public const int MaxRoomNodes = MaxRoomAgents + 1;
    public const string LocalNodeId = "drt-local";
    public static bool IsTerminal(Enums.RenderState state) => state is
        Enums.RenderState.Completed or Enums.RenderState.Cancelled or Enums.RenderState.Failed;
}

internal static class ContractGuard
{
    internal static void Require(bool condition, string message)
    {
        if (!condition) throw new ArgumentException(message);
    }

    internal static string Text(string? value, string name, int maxLength = 128)
    {
        Require(!string.IsNullOrWhiteSpace(value) && value.Length <= maxLength, $"Invalid {name}.");
        return value!.Trim();
    }

    internal static void Identity(int version, Guid runId)
    {
        Require(version == RenderProtocol.RoomPbrVersion, "Unsupported protocol version.");
        Require(runId != Guid.Empty, "RunId must not be empty.");
    }

    internal static void Dimensions(int width, int height)
    {
        Require(width > 0 && height > 0 && width <= 16384 && height <= 16384
            && (long)width * height <= 67108864, "Invalid image dimensions.");
    }
}
