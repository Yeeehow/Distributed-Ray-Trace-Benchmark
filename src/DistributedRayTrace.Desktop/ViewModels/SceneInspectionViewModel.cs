using System.Globalization;
using System.IO;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using DistributedRayTrace.Desktop.Services.Diagnostics;

namespace DistributedRayTrace.Desktop.ViewModels;

public enum SceneInspectionState { Idle, Inspecting, Loaded, LoadedWithReadinessLimitations, Failed, Cancelled }
public sealed record InspectionDisplayRow(string Label, string Value);

public sealed class SceneInspectionViewModel : ObservableObject, IAsyncDisposable
{
    private readonly Func<string?> _chooseScene;
    private readonly ISceneInspectionService _inspection;
    private Task? _shutdown;
    private string _scenePath = "";
    private string _sceneIndexText = "";
    private SceneInspectionState _state;
    private string _feedback = "Choose a GLB file to inspect with the Python backend.";
    private IReadOnlyList<InspectionDisplayRow> _values = Array.Empty<InspectionDisplayRow>();
    private string _issues = "";
    private CancellationTokenSource? _request;
    private Task _operation = Task.CompletedTask;
    private bool _disposed;

    public SceneInspectionViewModel(ISceneInspectionService inspection, IScenePathResolver paths, Func<string?> chooseScene)
    {
        _chooseScene = chooseScene;
        _inspection = inspection;
        try { _scenePath = paths.ResolveDefaultScene(); }
        catch (SceneInspectionException error) { _state = SceneInspectionState.Failed; _feedback = $"{error.Code}: {error.Message}"; }
        BrowseCommand = new RelayCommand(() => { if (_chooseScene() is { } path) ScenePath = path; }, () => CanEdit);
        InspectCommand = new AsyncRelayCommand(InspectAsync, () => CanEdit && !string.IsNullOrWhiteSpace(ScenePath));
        CancelCommand = new RelayCommand(() => { _request?.Cancel(); Refresh(); }, () => IsInspecting && _request?.IsCancellationRequested == false);
    }

    public IRelayCommand BrowseCommand { get; }
    public IAsyncRelayCommand InspectCommand { get; }
    public IRelayCommand CancelCommand { get; }
    public string ScenePath { get => _scenePath; set { if (SetProperty(ref _scenePath, value)) Refresh(); } }
    public string SceneIndexText { get => _sceneIndexText; set => SetProperty(ref _sceneIndexText, value); }
    public bool IsInspecting => _state == SceneInspectionState.Inspecting;
    public bool CanEdit => !_disposed && !IsInspecting;
    public string StatusText => _state == SceneInspectionState.LoadedWithReadinessLimitations ? "Loaded With Readiness Limitations" : _state.ToString();
    public string Feedback => _feedback;
    public IReadOnlyList<InspectionDisplayRow> Values => _values;
    public string Issues => _issues;

    private Task InspectAsync()
    {
        if (!CanEdit) return Task.CompletedTask;
        _operation = InspectCoreAsync();
        return _operation;
    }

    private async Task InspectCoreAsync()
    {
        int? index = null;
        _values = Array.Empty<InspectionDisplayRow>();
        _issues = "";
        if (!string.IsNullOrWhiteSpace(SceneIndexText))
        {
            if (!int.TryParse(SceneIndexText, NumberStyles.None, CultureInfo.InvariantCulture, out var parsed) || parsed < 0)
            {
                _state = SceneInspectionState.Failed;
                _feedback = "Scene Index must be a nonnegative integer, or blank for the document default.";
                Refresh();
                return;
            }
            index = parsed;
        }
        using var request = new CancellationTokenSource();
        _request = request;
        _state = SceneInspectionState.Inspecting;
        _feedback = "Reading scene metadata from Python…";
        Refresh();
        var path = ScenePath;
        try
        {
            if (!Path.IsPathFullyQualified(path))
                throw new SceneInspectionException("SCENE_CONFIGURATION", "Choose an absolute GLB path and a nonnegative scene index.");
            var summary = await _inspection.InspectAsync(path, index, request.Token);
            if (_disposed) return;
            request.Token.ThrowIfCancellationRequested();
            _state = summary.M3InputReady ? SceneInspectionState.Loaded : SceneInspectionState.LoadedWithReadinessLimitations;
            string Number(long value) => value.ToString("N0", CultureInfo.InvariantCulture);
            _values = new InspectionDisplayRow[]
            {
                new("Scene Index", Number(summary.SceneIndex)), new("Nodes", Number(summary.Nodes)),
                new("Mesh Instances", Number(summary.MeshInstances)), new("Triangles", Number(summary.Triangles)),
                new("Cameras", Number(summary.Cameras)), new("Lights", Number(summary.Lights)),
                new("Materials", Number(summary.Materials)), new("Textures", Number(summary.Textures)),
                new("Static Inputs Ready", summary.M3InputReady.ToString())
            };
            _issues = summary.Issues.Count == 0 ? "No validation issues." : string.Join(Environment.NewLine,
                summary.Issues.Select(issue => $"{issue.Severity}: {issue.Code} — {issue.Message}"));
            _feedback = "Python scene inspection completed. Readiness describes static inputs only.";
        }
        catch (OperationCanceledException)
        {
            if (!_disposed) { _state = SceneInspectionState.Cancelled; _feedback = "Scene inspection cancelled."; }
        }
        catch (SceneInspectionException error)
        {
            if (!_disposed) { _state = SceneInspectionState.Failed; _feedback = $"{error.Code}: {error.Message}"; }
        }
        catch (Exception)
        {
            if (!_disposed) { _state = SceneInspectionState.Failed; _feedback = "Scene inspection could not be completed."; }
        }
        finally
        {
            _request = null;
            if (!_disposed) Refresh();
        }
    }

    private void Refresh()
    {
        foreach (var name in new[] { nameof(StatusText), nameof(Feedback), nameof(Values), nameof(Issues), nameof(IsInspecting), nameof(CanEdit) })
            OnPropertyChanged(name);
        BrowseCommand.NotifyCanExecuteChanged();
        InspectCommand.NotifyCanExecuteChanged();
        CancelCommand.NotifyCanExecuteChanged();
    }

    public ValueTask DisposeAsync() => new(_shutdown ??= DisposeCoreAsync());

    private async Task DisposeCoreAsync()
    {
        if (_disposed) return;
        _disposed = true;
        _request?.Cancel();
        await _operation.WaitAsync(TimeSpan.FromSeconds(5));
    }
}
