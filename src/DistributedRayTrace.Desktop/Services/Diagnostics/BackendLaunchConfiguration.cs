using System.IO;

namespace DistributedRayTrace.Desktop.Services.Diagnostics;

public enum BackendLaunchMode { PythonModule, FrozenExecutable }

public sealed record BackendLaunchConfiguration(string ExecutablePath, BackendLaunchMode Mode,
    TimeSpan? Timeout = null, string? WorkingDirectory = null)
{
    public IReadOnlyList<string> ArgumentPrefix => Mode == BackendLaunchMode.PythonModule
        ? new[] { "-I", "-X", "utf8", "-m", "drt_backend" } : Array.Empty<string>();

    public void Validate()
    {
        if (!Enum.IsDefined(Mode) || !Path.IsPathFullyQualified(ExecutablePath) || !File.Exists(ExecutablePath)
            || (WorkingDirectory is not null && (!Path.IsPathFullyQualified(WorkingDirectory) || !Directory.Exists(WorkingDirectory))))
            throw new SceneInspectionException("BACKEND_CONFIGURATION", "Configure an existing absolute backend executable and valid working directory.");
    }

    public static BackendLaunchConfiguration Resolve(string applicationDirectory)
    {
        BackendLaunchConfiguration Checked(string executable, BackendLaunchMode mode)
        {
            var configuration = new BackendLaunchConfiguration(executable, mode, WorkingDirectory: applicationDirectory);
            configuration.Validate();
            return configuration;
        }
#if DEBUG
        if (Environment.GetEnvironmentVariable("DRT_BACKEND_EXECUTABLE") is { } frozen)
            return Checked(frozen, BackendLaunchMode.FrozenExecutable);
#endif
        var bundled = Path.Combine(applicationDirectory, "backend", "drt-backend.exe");
#if !DEBUG
        return Checked(bundled, BackendLaunchMode.FrozenExecutable);
#else
        if (File.Exists(bundled)) return Checked(bundled, BackendLaunchMode.FrozenExecutable);
        if (Environment.GetEnvironmentVariable("DRT_PYTHON_EXECUTABLE") is { } python)
            return Checked(python, BackendLaunchMode.PythonModule);
        return Checked(PythonSceneInspectionAdapter.ResolveInterpreter(applicationDirectory), BackendLaunchMode.PythonModule);
#endif
    }

    public static string ResolveDefaultScene(string applicationDirectory)
    {
#if DEBUG
        if (Environment.GetEnvironmentVariable("DRT_SCENE_PATH") is { } configured)
        {
            if (!Path.IsPathFullyQualified(configured) || !File.Exists(configured))
                throw new SceneInspectionException("SCENE_CONFIGURATION", "DRT_SCENE_PATH must name an existing absolute GLB file.");
            return configured;
        }
#endif
        var bundled = Path.Combine(applicationDirectory, "assets", "shogi_match_room_v3.glb");
        if (File.Exists(bundled)) return bundled;
        if (File.Exists(Path.Combine(applicationDirectory, "backend", "drt-backend.exe")))
            throw new SceneInspectionException("SCENE_CONFIGURATION", "The bundled scene is missing; choose a GLB file explicitly.");
#if !DEBUG
        throw new SceneInspectionException("SCENE_CONFIGURATION", "The bundled scene is missing; choose a GLB file explicitly.");
#else
        return Rendering.PythonRenderConfiguration.Development(applicationDirectory).Scene;
#endif
    }
}
