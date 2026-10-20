using System.IO;
using System.Security;
using System.Text.Json;
using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.Services.Settings;

public sealed class JsonSettingsStore : ISettingsStore
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true, MaxDepth = 16
    };

    public string? FilePath { get; }

    public JsonSettingsStore() : this(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData)) { }

    // An explicit application-data root permits isolated verification without touching user settings.
    public JsonSettingsStore(string applicationDataRoot)
    {
        if (!string.IsNullOrWhiteSpace(applicationDataRoot) && Path.IsPathFullyQualified(applicationDataRoot))
            FilePath = Path.Combine(applicationDataRoot, "DistributedRayTrace", "settings.json");
    }

    public Task<SettingsLoadResult> LoadAsync(CancellationToken cancellationToken = default) => Task.Run(async () =>
    {
        if (FilePath is null) return Fallback("Settings storage is unavailable. Using 160 × 90.");
        try
        {
            await using var stream = new FileStream(FilePath, FileMode.Open, FileAccess.Read, FileShare.Read,
                4096, FileOptions.Asynchronous);
            if (stream.Length > 65536) return Fallback("Settings file is too large. Using 160 × 90.");
            var settings = await JsonSerializer.DeserializeAsync<ApplicationSettings>(stream, JsonOptions, cancellationToken);
            if(settings is {SchemaVersion:1})
            {
                var migrated=ResolutionPreset.Find(settings.Width,settings.Height) is not null
                    ?settings with {SchemaVersion=2}:ApplicationSettings.Default;
                await stream.DisposeAsync();
                var saved=await SaveAsync(migrated,cancellationToken);
                return new SettingsLoadResult(migrated,saved.Success?"Settings migrated to the supported PBR resolutions.":
                    "Using migrated PBR settings in memory. "+saved.Error);
            }
            return settings is { IsValid: true } ? new SettingsLoadResult(settings)
                : Fallback("Saved resolution or settings version is unsupported. Using 160 × 90.");
        }
        catch (Exception error) when (error is FileNotFoundException or DirectoryNotFoundException)
        {
            return new SettingsLoadResult(ApplicationSettings.Default);
        }
        catch (Exception error) when (IsStorageFailure(error))
        {
            return Fallback("Saved settings could not be read. Using 160 × 90.");
        }
    }, cancellationToken);

    public Task<SettingsSaveResult> SaveAsync(ApplicationSettings settings, CancellationToken cancellationToken = default) => Task.Run(async () =>
    {
        if (!settings.IsValid) return new SettingsSaveResult(false, "Select a supported resolution preset.");
        if (FilePath is null) return new SettingsSaveResult(false, "Settings storage is unavailable. Your resolution was not saved.");
        string? temporaryPath = null;
        try
        {
            var directory = Path.GetDirectoryName(FilePath)!;
            Directory.CreateDirectory(directory);
            temporaryPath = Path.Combine(directory, $"settings.{Guid.NewGuid():N}.tmp");
            await using (var stream = new FileStream(temporaryPath, FileMode.CreateNew, FileAccess.Write,
                FileShare.None, 4096, FileOptions.Asynchronous | FileOptions.WriteThrough))
            {
                await JsonSerializer.SerializeAsync(stream, settings, JsonOptions, cancellationToken);
                await stream.FlushAsync(cancellationToken);
                stream.Flush(flushToDisk: true);
            }
            cancellationToken.ThrowIfCancellationRequested();
            if (File.Exists(FilePath)) File.Replace(temporaryPath, FilePath, null);
            else File.Move(temporaryPath, FilePath);
            return new SettingsSaveResult(true);
        }
        catch (Exception error) when (IsStorageFailure(error))
        {
            return new SettingsSaveResult(false, "Settings could not be saved. Your previous resolution is unchanged. " + error.Message);
        }
        finally
        {
            if (temporaryPath is not null)
            {
                try { File.Delete(temporaryPath); }
                catch (Exception error) when (IsStorageFailure(error)) { /* Best-effort cleanup of our temporary file only. */ }
            }
        }
    }, cancellationToken);

    private static SettingsLoadResult Fallback(string warning) => new(ApplicationSettings.Default, warning);

    private static bool IsStorageFailure(Exception error) => error is IOException or UnauthorizedAccessException
        or SecurityException or JsonException or NotSupportedException or ArgumentException;
}
