using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;

namespace DistributedRayTrace.Desktop.Services.Diagnostics;

public sealed class PythonSceneInspectionAdapter
{
    private readonly BackendLaunchConfiguration _configuration;
    private readonly TimeSpan _timeout;
    public const int MaxOutputCharacters = 1024 * 1024;
    public const int MaxErrorCharacters = 64 * 1024;

    public PythonSceneInspectionAdapter(string interpreter, TimeSpan? timeout = null)
        : this(new BackendLaunchConfiguration(interpreter, BackendLaunchMode.PythonModule, timeout)) { }

    public PythonSceneInspectionAdapter(BackendLaunchConfiguration configuration)
    {
        configuration.Validate();
        _configuration = configuration;
        _timeout = configuration.Timeout ?? TimeSpan.FromSeconds(30);
        if (_timeout <= TimeSpan.Zero || _timeout > TimeSpan.FromMinutes(5)) throw new ArgumentOutOfRangeException(nameof(configuration));
    }

    public static string ResolveInterpreter(string applicationDirectory)
    {
        var configured = Environment.GetEnvironmentVariable("DRT_PYTHON_EXECUTABLE");
        if (!string.IsNullOrWhiteSpace(configured)) return configured;
        DirectoryInfo? directory = new(applicationDirectory);
        for (var depth = 0; depth < 8 && directory is not null; depth++, directory = directory.Parent)
        {
            var project = Path.Combine(directory.FullName, "DistributedRayTrace");
            var backend = Path.Combine(project, "python_backend");
            var candidate = Path.Combine(backend, ".venv", "Scripts", "python.exe");
            if (File.Exists(Path.Combine(project, "DistributedRayTrace.sln"))
                && File.Exists(Path.Combine(backend, "pyproject.toml")) && File.Exists(candidate)) return candidate;
        }
        throw new SceneInspectionException("INTERPRETER_CONFIGURATION", "Set DRT_PYTHON_EXECUTABLE; no development virtual environment was found.");
    }

    public async Task<SceneInspectionSummary> InspectAsync(string scenePath, int? sceneIndex = null,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!Path.IsPathFullyQualified(scenePath) || sceneIndex < 0)
            throw new SceneInspectionException("SCENE_CONFIGURATION", "Choose an absolute GLB path and a nonnegative scene index.");
        var arguments=_configuration.ArgumentPrefix.Concat(new[]{"inspect-scene","--scene",scenePath,"--json"}).ToList();
        if(sceneIndex is {} index) { arguments.Add("--scene-index");arguments.Add(index.ToString(CultureInfo.InvariantCulture)); }
        using var deadline=new CancellationTokenSource(_timeout);
        using var lifetime=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken,deadline.Token);
        Rendering.OwnedPythonProcess process;
        try { process=new Rendering.OwnedPythonProcess(_configuration.ExecutablePath,arguments,_configuration.WorkingDirectory??AppContext.BaseDirectory); }
        catch(Exception error) when(error is Win32Exception or IOException)
        { throw new SceneInspectionException("PROCESS_START_FAILED","Python inspection could not be started: "+error.Message); }
        using(process)
        {
            var stdout=ReadBoundedAsync(process.Output,MaxOutputCharacters,lifetime.Token);
            var stderr=ReadBoundedAsync(process.Error,MaxErrorCharacters,lifetime.Token);
            try
            {
                await Task.WhenAll(stdout,stderr,process.WaitForExitAsync(_timeout)).WaitAsync(lifetime.Token).ConfigureAwait(false);
                cancellationToken.ThrowIfCancellationRequested();
                if(process.ExitCode is not (0 or 3 or 4)) throw new SceneInspectionException("PROCESS_FAILED","Python inspection exited without a valid response.");
                return SceneInspectionSummary.ParseResponse(await stdout.ConfigureAwait(false),process.ExitCode);
            }
            catch(OperationCanceledException) when(!cancellationToken.IsCancellationRequested && deadline.IsCancellationRequested)
            { throw new SceneInspectionException("INSPECTION_TIMEOUT","Python inspection exceeded its time limit."); }
            finally
            {
                lifetime.Cancel();
                if(process.ProcessIds().Length!=0) await process.TerminateAndVerifyAsync().ConfigureAwait(false);
                process.Dispose();
                try { await Task.WhenAll(stdout,stderr).WaitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false); }
                catch(Exception error) when(error is OperationCanceledException or IOException or ObjectDisposedException or SceneInspectionException) { }
            }
        }
    }

    private static async Task<string> ReadBoundedAsync(StreamReader reader, int limit, CancellationToken token)
    {
        var output = new StringBuilder();
        var buffer = new char[4096];
        int count;
        while ((count = await reader.ReadAsync(buffer.AsMemory(), token).ConfigureAwait(false)) != 0)
        {
            if (output.Length + count > limit)
                throw new SceneInspectionException("OUTPUT_LIMIT", "Python diagnostic output exceeded its size limit.");
            output.Append(buffer, 0, count);
        }
        return output.ToString();
    }
}
