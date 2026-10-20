namespace DistributedRayTrace.Desktop.Models;

public sealed record SystemInformation(
    string Processor, string CoresAndClock, string Gpu, string OperatingSystem, string Info,
    string ProcessorDetails, string CoresDetails, string GpuDetails)
{
    public static SystemInformation Unavailable { get; } = new(
        "Unavailable", "Unavailable", "Unavailable", "Unavailable", "Unavailable",
        "System information could not be read.", "System information could not be read.",
        "Adapter information could not be read.");
}
