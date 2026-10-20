using System.Globalization;
using System.Runtime.InteropServices;
using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.Services.SystemInfo;

public sealed class WindowsSystemInfoProvider : ISystemInfoProvider
{
    private readonly IWmiQuery _query;

    public WindowsSystemInfoProvider(IWmiQuery? query = null) => _query = query ?? new WindowsWmiQuery();

    public Task<SystemInformation> GetSnapshotAsync(CancellationToken cancellationToken = default) =>
        Task.Run(() => ReadSnapshot(cancellationToken), cancellationToken);

    private SystemInformation ReadSnapshot(CancellationToken token)
    {
        var cpus = Read("Win32_Processor", token, "Name", "NumberOfCores", "NumberOfLogicalProcessors", "MaxClockSpeed");
        var gpus = Read("Win32_VideoController", token, "Name");
        var systems = Read("Win32_OperatingSystem", token, "Caption", "Version", "BuildNumber");
        var memory = Read("Win32_PhysicalMemory", token, "Capacity");

        var cpuNames = cpus.Select(cpu => Text(cpu, "Name") ?? "Unavailable")
            .GroupBy(name => name, StringComparer.OrdinalIgnoreCase)
            .Select(group => group.Count() > 1 ? $"{group.Key} (×{group.Count()})" : group.Key);
        var processor = cpus.Count == 0 ? "Unavailable" : string.Join("; ", cpuNames);
        var physical = SumComplete(cpus, "NumberOfCores");
        var logical = SumComplete(cpus, "NumberOfLogicalProcessors");
        var clocks = cpus.Select(cpu => Number(cpu, "MaxClockSpeed")).Where(value => value.HasValue).ToArray();
        var clock = clocks.Length == 0 ? "clock unavailable"
            : $"up to {(clocks.Max()!.Value / 1000m).ToString("0.##", CultureInfo.InvariantCulture)} GHz (reported)";
        var coreText = physical.HasValue ? $"{physical} physical" : "Physical cores unavailable";
        var logicalText = logical.HasValue ? $"{logical} logical"
            : $"{Environment.ProcessorCount} logical available to process";

        var gpuNames = gpus.Select(gpu => Text(gpu, "Name")).OfType<string>()
            .Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        var gpuText = gpuNames.Length == 0 ? "Unavailable" : string.Join("\n", gpuNames.Take(2));
        if (gpuNames.Length > 2) gpuText += $"\n(+{gpuNames.Length - 2} more adapters)";

        var os = systems.FirstOrDefault();
        var caption = os is null ? null : Text(os, "Caption");
        var version = os is null ? null : Text(os, "Version");
        var build = os is null ? null : Text(os, "BuildNumber");
        var osText = caption ?? RuntimeInformation.OSDescription;
        if (version is not null) osText += $"\nVersion {version}";
        if (build is not null) osText += $" · build {build}";
        var ram = SumComplete(memory, "Capacity");
        var ramText = ram.HasValue
            ? $"{(ram.Value / 1073741824m).ToString("0.##", CultureInfo.InvariantCulture)} GiB installed RAM"
            : "RAM unavailable";
        token.ThrowIfCancellationRequested();
        return new SystemInformation(processor, $"{coreText} / {logicalText} · {clock}", gpuText, osText,
            $"{ramText} · {RuntimeInformation.OSArchitecture} OS",
            cpus.Count == 0 ? "CPU model unavailable from WMI." : $"{cpus.Count} CPU package(s) reported by WMI.\n{processor}",
            "Counts are summed across all CPU packages when complete. Process-available logical counts are explicitly labelled. " +
            "Clock is WMI MaxClockSpeed, a reported maximum, not a live measurement. Software workers are separate from hardware cores.",
            gpuNames.Length == 0 ? "GPU adapter names unavailable from WMI." : string.Join("\n", gpuNames));
    }

    private IReadOnlyList<IReadOnlyDictionary<string, object?>> Read(string name, CancellationToken token, params string[] properties)
    {
        token.ThrowIfCancellationRequested();
        try { return _query.Read(name, properties); }
        catch (Exception error) when (WindowsWmiQuery.IsUnavailable(error)) { return []; }
    }

    private static string? Text(IReadOnlyDictionary<string, object?> row, string property)
    {
        if (!row.TryGetValue(property, out var value) || value is not string text || string.IsNullOrWhiteSpace(text)) return null;
        return string.Join(" ", text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
    }

    private static ulong? Number(IReadOnlyDictionary<string, object?> row, string property)
    {
        if (!row.TryGetValue(property, out var value) || value is null) return null;
        try { var number = Convert.ToUInt64(value, CultureInfo.InvariantCulture); return number > 0 ? number : null; }
        catch (Exception error) when (error is FormatException or InvalidCastException or OverflowException) { return null; }
    }

    private static ulong? SumComplete(IReadOnlyList<IReadOnlyDictionary<string, object?>> rows, string property)
    {
        if (rows.Count == 0) return null;
        ulong total = 0;
        foreach (var row in rows)
        {
            var value = Number(row, property);
            if (!value.HasValue) return null; // Never present a partial sum as a machine total.
            try { total = checked(total + value.Value); }
            catch (OverflowException) { return null; }
        }
        return total;
    }
}
