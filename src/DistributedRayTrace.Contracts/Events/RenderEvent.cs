using System.Buffers.Binary;
using System.Text.Json.Serialization;
using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Models;

namespace DistributedRayTrace.Contracts.Events;

[JsonPolymorphic(TypeDiscriminatorPropertyName = "type")]
[JsonDerivedType(typeof(RoomEvent), "roomEvent")]
[JsonDerivedType(typeof(RoomFrameEvent), "roomFrame")]
public abstract record RenderEvent
{
    public int ProtocolVersion { get; }
    public Guid RunId { get; }
    public long Sequence { get; }

    protected RenderEvent(int protocolVersion, Guid runId, long sequence)
    {
        ContractGuard.Identity(protocolVersion, runId);
        ContractGuard.Require(sequence > 0, "Event sequence must be positive.");
        ProtocolVersion = protocolVersion;
        RunId = runId;
        Sequence = sequence;
    }
}

public sealed record RenderProgress : RenderEvent
{
    public long CompletedSamples { get; }
    public long? TargetSamples { get; }
    public double? Fraction { get; }
    public RenderProgress(int protocolVersion, Guid runId, long sequence, long completedSamples, long? targetSamples, double? fraction)
        : base(protocolVersion, runId, sequence)
    {
        ContractGuard.Require(completedSamples >= 0 && (targetSamples is null || targetSamples > 0 && completedSamples <= targetSamples), "Invalid sample counts.");
        ContractGuard.Require(fraction is null || double.IsFinite(fraction.Value) && fraction >= 0 && fraction <= 1, "Invalid progress fraction.");
        ContractGuard.Require(targetSamples is null || fraction is null
            || Math.Abs(fraction.Value - (double)completedSamples / targetSamples.Value) <= 1e-9, "Fraction disagrees with sample counts.");
        CompletedSamples = completedSamples;
        TargetSamples = targetSamples;
        Fraction = fraction ?? (targetSamples.HasValue ? (double)completedSamples / targetSamples.Value : null);
    }
}

public sealed record RenderFrame : RenderEvent
{
    private readonly byte[] _pngBytes;
    public int Width { get; }
    public int Height { get; }
    public ImageEncoding Encoding { get; }
    public byte[] PngBytes => (byte[])_pngBytes.Clone();

    public RenderFrame(int protocolVersion, Guid runId, long sequence, int width, int height, ImageEncoding encoding, byte[] pngBytes)
        : base(protocolVersion, runId, sequence)
    {
        ContractGuard.Dimensions(width, height);
        ContractGuard.Require(encoding == ImageEncoding.Png, "Only PNG is supported.");
        ContractGuard.Require(pngBytes is { Length: >= 33 and <= RenderProtocol.MaxFrameBytes }, "Invalid PNG payload size.");
        _pngBytes = (byte[])pngBytes.Clone();
        ReadOnlySpan<byte> signature = [137, 80, 78, 71, 13, 10, 26, 10];
        ContractGuard.Require(_pngBytes.AsSpan(0, 8).SequenceEqual(signature)
            && BinaryPrimitives.ReadUInt32BigEndian(_pngBytes.AsSpan(8, 4)) == 13
            && _pngBytes.AsSpan(12, 4).SequenceEqual("IHDR"u8)
            && BinaryPrimitives.ReadUInt32BigEndian(_pngBytes.AsSpan(16, 4)) == width
            && BinaryPrimitives.ReadUInt32BigEndian(_pngBytes.AsSpan(20, 4)) == height, "PNG header does not match frame metadata.");
        Width = width;
        Height = height;
        Encoding = encoding;
    }
}

public sealed record RenderContribution : RenderEvent
{
    public string ContributorId { get; }
    public ContributorType ContributorType { get; }
    public long AcceptedRayCount { get; }
    public RenderContribution(int protocolVersion, Guid runId, long sequence, string contributorId,
        ContributorType contributorType, long acceptedRayCount) : base(protocolVersion, runId, sequence)
    {
        ContractGuard.Require(Enum.IsDefined(contributorType) && acceptedRayCount >= 0, "Invalid contribution.");
        ContributorId = ContractGuard.Text(contributorId, nameof(contributorId));
        ContributorType = contributorType;
        AcceptedRayCount = acceptedRayCount;
    }
}

