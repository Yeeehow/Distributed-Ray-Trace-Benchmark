using System.Management;
using System.Runtime.InteropServices;
using System.Security;

namespace DistributedRayTrace.Desktop.Services.SystemInfo;

public interface IWmiQuery
{
    IReadOnlyList<IReadOnlyDictionary<string, object?>> Read(string className, params string[] properties);
}

public sealed class WindowsWmiQuery : IWmiQuery
{
    public IReadOnlyList<IReadOnlyDictionary<string, object?>> Read(string className, params string[] properties)
    {
        using var searcher = new ManagementObjectSearcher("root\\CIMV2",
            $"SELECT {string.Join(", ", properties)} FROM {className}");
        searcher.Options.Timeout = TimeSpan.FromSeconds(5);
        using var results = searcher.Get();
        var rows = new List<IReadOnlyDictionary<string, object?>>();
        foreach (ManagementBaseObject result in results)
        {
            using (result)
            {
                var row = new Dictionary<string, object?>();
                foreach (var property in properties)
                {
                    try { row[property] = result[property]; }
                    catch (Exception error) when (IsUnavailable(error)) { row[property] = null; }
                }
                rows.Add(row);
            }
        }
        return rows;
    }

    internal static bool IsUnavailable(Exception error) => error is ManagementException or COMException
        or UnauthorizedAccessException or SecurityException or InvalidOperationException
        or NotSupportedException or ArgumentException;
}
