using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace DistributedRayTrace.Desktop.Services.Cache;

public sealed record CacheClearResult(int FilesDeleted, int DirectoriesDeleted);

public interface ILocalRenderCache
{
    string DirectoryPath { get; }
    CacheClearResult Clear(CancellationToken cancellationToken = default);
}

/// <summary>Only the fixed user-data-relative renders directory is a cache deletion target.</summary>
public sealed class LocalRenderCache : ILocalRenderCache
{
    private const uint ReadAttributes = 0x80, ReadDataOrListDirectory = 1, DeleteAccess = 0x10000;
    private const uint ShareRead = 1, ShareWrite = 2;
    private const uint OpenExisting = 3, OpenReparsePoint = 0x00200000, BackupSemantics = 0x02000000;
    private const int MaximumEntries = 16384, MaximumDepth = 64;
    private readonly string _applicationDataRoot;
    public string DirectoryPath { get; }
    public static string DefaultDirectory => new LocalRenderCache().DirectoryPath;

    public LocalRenderCache() : this(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData)) { }

    // Explicit roots support isolated verification. No UI, environment variable or output path supplies a deletion target.
    public LocalRenderCache(string applicationDataRoot)
    {
        if (string.IsNullOrWhiteSpace(applicationDataRoot) || !Path.IsPathFullyQualified(applicationDataRoot))
            throw new ArgumentException("A full local application-data root is required.");
        _applicationDataRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(applicationDataRoot));
        var drive = Path.GetPathRoot(_applicationDataRoot)!;
        if (drive.Length != 3 || !char.IsAsciiLetter(drive[0]) || drive[1] != ':' || drive[2] != '\\')
            throw new ArgumentException("Cache cleanup requires a local drive path.");
        DirectoryPath = Path.GetFullPath(Path.Combine(_applicationDataRoot, "DistributedRayTrace", "renders"));
        if (!DirectoryPath.StartsWith(_applicationDataRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Cache path is outside application data.");
    }

    public static IDisposable? AcquireForOutput(string outputDirectory)
    {
        var cache = new LocalRenderCache();
        return Path.IsPathFullyQualified(outputDirectory)
            && string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(outputDirectory)), cache.DirectoryPath, StringComparison.OrdinalIgnoreCase)
            ? cache.AcquireRenderLease() : null;
    }

    /// <summary>Held for the entire backend stream, including child cleanup and final evidence writes.</summary>
    public IDisposable AcquireRenderLease() => OpenRoot(forDeletion: false, create: true)!;

    public CacheClearResult Clear(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        using var root = OpenRoot(forDeletion: true, create: false);
        if (root is null) return new(0, 0);
        var entries = new List<Entry>();
        try
        {
            // Preflight and pin the entire snapshot before deleting anything. Each ancestor stays open
            // without write/delete sharing, so components cannot be replaced or changed into reparse points mid-walk.
            Collect(DirectoryPath, entries, 0, cancellationToken);
            var files = 0; var directories = 0;
            foreach (var entry in entries.AsEnumerable().Reverse())
            {
                cancellationToken.ThrowIfCancellationRequested();
                var disposition = new Disposition { DeleteFile = 1 };
                if (!SetFileInformationByHandle(entry.Handle, 4, ref disposition, 1))
                    throw Error("Cache cleanup stopped; some cache files may remain.");
                entry.Handle.Dispose(); // Complete this unlink before deleting its containing directory.
                if (entry.IsDirectory) directories++; else files++;
            }
            if (Directory.EnumerateFileSystemEntries(DirectoryPath).Any())
                throw new IOException("The cache changed during cleanup; some cache files remain.");
            // Keep the empty renders directory as the cross-process activity-lock anchor.
            return new(files, directories);
        }
        finally { foreach (var entry in entries) entry.Handle.Dispose(); }
    }

    private void Collect(string directory, List<Entry> entries, int depth, CancellationToken token)
    {
        if (depth > MaximumDepth) throw new IOException("Cache directory nesting exceeds the safe cleanup limit.");
        foreach (var path in Directory.EnumerateFileSystemEntries(directory))
        {
            token.ThrowIfCancellationRequested();
            if (entries.Count >= MaximumEntries) throw new IOException("Cache entry count exceeds the safe cleanup limit.");
            var full = Path.GetFullPath(path);
            if (!full.StartsWith(DirectoryPath + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                || !string.Equals(Path.GetDirectoryName(full), directory, StringComparison.OrdinalIgnoreCase))
                throw new IOException("Cache entry is outside the expected directory.");
            // No write/delete sharing: busy files fail preflight. OPEN_REPARSE_POINT opens the link itself.
            var handle = Open(full, ReadAttributes | ReadDataOrListDirectory | DeleteAccess, ShareRead, allowMissing: false)!;
            try
            {
                var attributes = Attributes(handle);
                RejectLinkOrReadOnly(attributes);
                var isDirectory = (attributes & FileAttributes.Directory) != 0;
                entries.Add(new(handle, isDirectory));
                handle = null!;
                if (isDirectory) Collect(full, entries, depth + 1, token);
            }
            finally { handle?.Dispose(); }
        }
    }

    private Pins? OpenRoot(bool forDeletion, bool create)
    {
        var pins = new Pins();
        try
        {
            var drive = Path.GetPathRoot(_applicationDataRoot)!;
            var parent = drive;
            var components = _applicationDataRoot[drive.Length..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries)
                .Concat(new[] { "DistributedRayTrace", "renders" }).ToArray();
            PinDirectory(drive, ReadAttributes | ReadDataOrListDirectory, false);
            for (var index = 0; index < components.Length; index++)
            {
                parent = Path.Combine(parent, components[index]);
                var last = index == components.Length - 1;
                if (create && index >= components.Length - 2) Directory.CreateDirectory(parent);
                // Attribute-only handles do not participate in Windows share-access checks.
                // LIST_DIRECTORY makes the no-delete sharing rule pin each ancestor and the root.
                if (!PinDirectory(parent, ReadAttributes | ReadDataOrListDirectory | (last && forDeletion ? DeleteAccess : 0), true))
                { pins.Dispose(); return null; }
            }
            return pins;
        }
        catch { pins.Dispose(); throw; }

        bool PinDirectory(string path, uint access, bool allowMissing)
        {
            var handle = Open(path, access, forDeletion ? ShareRead : ShareRead | ShareWrite, allowMissing);
            if (handle is null) return false;
            pins.Handles.Add(handle);
            var attributes = Attributes(handle);
            if ((attributes & FileAttributes.ReparsePoint) != 0 || (attributes & FileAttributes.Directory) == 0)
                throw new IOException("Cache cleanup refuses directory links, junctions and non-directory paths.");
            if (forDeletion && (access & DeleteAccess) != 0) RejectLinkOrReadOnly(attributes);
            return true;
        }
    }

    private static SafeFileHandle? Open(string path, uint access, uint share, bool allowMissing)
    {
        var handle = CreateFile(path, access, share, IntPtr.Zero, OpenExisting, OpenReparsePoint | BackupSemantics, IntPtr.Zero);
        if (!handle.IsInvalid) return handle;
        var error = Marshal.GetLastWin32Error(); handle.Dispose();
        if (allowMissing && error is 2 or 3) return null;
        throw new IOException("Cannot safely access the cache. Close other renders or files using it. " + new Win32Exception(error).Message);
    }

    private static FileAttributes Attributes(SafeFileHandle handle)
    {
        if (!GetFileInformationByHandle(handle, out var info)) throw Error("Cannot verify cache entry attributes.");
        return (FileAttributes)info.Attributes;
    }
    private static void RejectLinkOrReadOnly(FileAttributes attributes)
    {
        if ((attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("Cache cleanup refuses symbolic links and junctions.");
        if ((attributes & FileAttributes.ReadOnly) != 0) throw new IOException("Cache cleanup refuses read-only entries.");
    }
    private static IOException Error(string text) => new(text + " " + new Win32Exception(Marshal.GetLastWin32Error()).Message);
    private sealed record Entry(SafeFileHandle Handle, bool IsDirectory);
    private sealed class Pins : IDisposable
    {
        public List<SafeFileHandle> Handles { get; } = [];
        public void Dispose() { foreach (var handle in Handles.AsEnumerable().Reverse()) handle.Dispose(); }
    }
    [StructLayout(LayoutKind.Sequential)] private struct Disposition { public byte DeleteFile; }
    [StructLayout(LayoutKind.Sequential)] private struct FileInformation
    {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileInformation information);
    [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle file, int infoClass, ref Disposition information, uint size);
}
