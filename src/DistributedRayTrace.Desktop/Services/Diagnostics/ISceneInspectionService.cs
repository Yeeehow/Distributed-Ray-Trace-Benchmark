namespace DistributedRayTrace.Desktop.Services.Diagnostics;

public interface ISceneInspectionService
{
    Task<SceneInspectionSummary> InspectAsync(string scenePath, int? sceneIndex, CancellationToken cancellationToken);
}

public interface IScenePathResolver
{
    string ResolveDefaultScene();
}

/// <summary>Keep launch resolution and the existing real diagnostic adapter out of presentation code.</summary>
public sealed class PythonSceneInspectionService(string applicationDirectory) : ISceneInspectionService
{
    public Task<SceneInspectionSummary> InspectAsync(string scenePath, int? sceneIndex, CancellationToken cancellationToken) =>
        Task.Run(() => new PythonSceneInspectionAdapter(BackendLaunchConfiguration.Resolve(applicationDirectory))
            .InspectAsync(scenePath, sceneIndex, cancellationToken), cancellationToken);
}

public sealed class ApplicationScenePathResolver(string applicationDirectory) : IScenePathResolver
{
    public string ResolveDefaultScene() => BackendLaunchConfiguration.ResolveDefaultScene(applicationDirectory);
}
