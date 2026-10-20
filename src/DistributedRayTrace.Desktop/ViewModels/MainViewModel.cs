using System.Windows.Input;
using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using DistributedRayTrace.Contracts.Enums;
using DistributedRayTrace.Contracts.Events;
using DistributedRayTrace.Contracts.Models;
using DistributedRayTrace.Desktop.Models;
using DistributedRayTrace.Desktop.Services.Dialogs;
using DistributedRayTrace.Desktop.Services.Cache;
using DistributedRayTrace.Desktop.Services.Rendering;
using DistributedRayTrace.Desktop.Services.Settings;
using DistributedRayTrace.Desktop.Services.SystemInfo;

namespace DistributedRayTrace.Desktop.ViewModels;

public enum BackendAvailability { NotImplemented, Disconnected, Available }
public enum PreviewState { Unavailable, StaticReference, RealFinal, RealProvisional }
public sealed record ModeDisplayState(RenderMode Mode, RenderState State, TimeSpan? Elapsed)
{
    public string ElapsedText => Elapsed is { } value ? $"{value.TotalSeconds:0.0} s" : "—";
}

public sealed partial class MainViewModel : ObservableObject, IAsyncDisposable
{
    private readonly ISystemInfoProvider _systemInfoProvider;
    private readonly ISettingsStore _settingsStore;
    private readonly ISettingsDialog _settingsDialog;
    private readonly RenderSessionController _sessions;
    private readonly Action<Action> _dispatch;
    private readonly CancellationTokenSource _lifetime=new();
    private ApplicationSettings _settings=ApplicationSettings.Default;
    private SystemInformation _systemInformation=SystemInformation.Unavailable;
    private RenderSessionSnapshot _session;
    private PreviewState _preview;
    private string? _settingsNotice,_lifecycleError,_previewError;
    private bool _initialized,_disposed;
    private Task? _initialization,_shutdown;
    private int _updateQueued;
    private Guid? _presentedRunId;
    private long _presentedSequence;

    public MainViewModel(ISystemInfoProvider systemInfoProvider,ISettingsStore settingsStore,ISettingsDialog settingsDialog,
        RenderSessionController sessions,Action<Action>? dispatch=null,Action<HelpTopic>? showHelp=null,
        ILocalRenderCache? localCache=null,ILocalCacheDialogs? cacheDialogs=null)
    {
        _systemInfoProvider=systemInfoProvider;_settingsStore=settingsStore;_settingsDialog=settingsDialog;
        _sessions=sessions;_dispatch=dispatch??(action=>action());_session=sessions.Snapshot;
        StartLocalCommand=new AsyncRelayCommand(()=>StartSelectedAsync(RenderMode.CpuSerialPreview),CanStart);
        ConnectRemoteCommand=new AsyncRelayCommand(()=>RemoteOperationAsync(()=>_sessions.ConnectRoomAgentsAsync(Settings.Width,Settings.Height,_lifetime.Token)),CanManageRemote);
        DisconnectRemoteCommand=new AsyncRelayCommand(()=>RemoteOperationAsync(_sessions.DisconnectRoomAgentsAsync),CanManageRemote);
        StartDistributedCommand=new AsyncRelayCommand(()=>StartSelectedAsync(RenderMode.DistributedAllCore),()=>CanStart() && SelectedDistributedAvailable);
        StartAllCoreCommand=new AsyncRelayCommand(()=>StartSelectedAsync(RenderMode.LocalAllCore),()=>CanStart() && _sessions.IsLocalParallelAvailable);
        CancelCommand=new AsyncRelayCommand(()=>_sessions.RequestCancellationAsync(),()=>!_disposed && State is RenderState.Preparing or RenderState.Rendering);
        OpenSettingsCommand=new RelayCommand(OpenSettings,()=>!_disposed && _initialized && !IsSessionActive && !_cacheBusy);
        InitializeHelp(showHelp,localCache,cacheDialogs);_sessions.Changed+=Sessions_Changed;
    }
    public IAsyncRelayCommand StartLocalCommand { get; }
    public IAsyncRelayCommand StartDistributedCommand { get; }
    public IAsyncRelayCommand StartAllCoreCommand { get; }
    public IAsyncRelayCommand CancelCommand { get; }
    public IRelayCommand OpenSettingsCommand { get; }
    public bool IsSessionActive=>State is RenderState.Preparing or RenderState.Rendering or RenderState.Cancelling;
    public bool IsLocalActive=>IsSessionActive;
    public bool IsDistributedActive=>IsSessionActive && _session.Request?.Mode==RenderMode.DistributedAllCore;
    public bool IsSerialActive=>IsSessionActive && _session.Request?.Mode==RenderMode.CpuSerialPreview;
    public bool IsParallelActive=>IsSessionActive && _session.Request?.Mode==RenderMode.LocalAllCore;
    public ICommand LocalActionCommand=>IsSerialActive?CancelCommand:StartLocalCommand;
    public string LocalActionText=>IsSerialActive?"Cancel":"Start";
    public string LocalActionName=>IsSerialActive?"Cancel CPU Serial Preview":"Start real CPU Serial Preview";
    public ICommand AllCoreActionCommand=>IsParallelActive?CancelCommand:StartAllCoreCommand;
    public string AllCoreActionText=>IsParallelActive?"Cancel":"Start";
    public string AllCoreActionName=>IsParallelActive?"Cancel CPU Local All Core":"Start real CPU Local All Core";
    public ICommand DistributedActionCommand=>IsDistributedActive?CancelCommand:StartDistributedCommand;
    public string DistributedActionText=>IsDistributedActive?"Cancel":SelectedDistributedAvailable?"Start":"Unavailable";
    public string DistributedActionName=>IsDistributedActive?"Cancel CPU Distributed All Core":"Start authenticated CPU Distributed All Core";
    public string RemoteSystemsText=>_sessions.RemotePeers.Any(p=>p.Connected && p.Authenticated && p.Session is not null)
        ?string.Join("\n",_sessions.RemotePeers.Where(p=>p.Connected && p.Authenticated && p.Session is not null).Select(p=>$"{p.Id} · connected"))
        :"0 live connected systems";
    public string RemoteStatus=>_sessions.RoomAgentStatus;
    public string DistributedScoreText=>Completed(RenderMode.DistributedAllCore)?.ScoreText??"—";
    public string DistributedCapacityText=>"Local + selected remote CPUs · up to 64 Workers per machine / 256 total";
    public ApplicationSettings Settings=>_settings;
    public SystemInformation SystemInformation=>_systemInformation;
    public RenderSessionSnapshot Session=>_session;
    public RenderState State=>_session.State;
    public string StatusText=>State.ToString();
    public BackendAvailability Backend=>_sessions.IsBackendAvailable?BackendAvailability.Available:BackendAvailability.Disconnected;
    public string BackendStatus=>_sessions.IsBackendAvailable?"Python PBR · Serial / Local / authenticated LAN":"Python unavailable";
    public ModeDisplayState Local=>new(RenderMode.CpuSerialPreview,State,Elapsed);
    public ModeDisplayState Distributed=>new(RenderMode.DistributedAllCore,IsDistributedActive?State:RenderState.Idle,IsDistributedActive?Elapsed:null);
    private RelativePbrPerformance Relative=>RelativePbrPerformance.Compare(Completed(RenderMode.LocalAllCore),Completed(RenderMode.DistributedAllCore));
    public string EfficiencyText=>Relative.ChangeDisplay;
    public string ComparisonNotice=>Relative.Notice;
    private CompletedPbrResult? Completed(RenderMode mode)=>_sessions.LastSuccessfulResults.GetValueOrDefault(mode);
    public string SerialScoreText=>Completed(RenderMode.CpuSerialPreview)?.ScoreText??"—";
    public string LocalScoreText=>Completed(RenderMode.LocalAllCore)?.ScoreText??"—";
    public string SerialResultDetail=>Completed(RenderMode.CpuSerialPreview)?.Detail??"No successful Serial result yet";
    public string LocalResultDetail=>Completed(RenderMode.LocalAllCore)?.Detail??"No successful Local result yet";
    public string DistributedResultDetail=>Completed(RenderMode.DistributedAllCore)?.Detail??"No successful Distributed result yet";
    public IReadOnlyList<RenderContribution> Contributions=>_session.Contributions.OrderByDescending(c=>c.AcceptedRayCount).ThenBy(c=>c.ContributorId,StringComparer.Ordinal).ToArray();
    private WorkerContributionRanking Ranking=>IsSessionActive && _session.Room is {} current && _session.Request is {} request
        ?WorkerContributionRanking.Create(request.Mode,current.Contributions,nodes:current.Nodes)
        :_sessions.MostRecentSuccessful is {} last?WorkerContributionRanking.Create(last.Mode,last.Contributions,
            checked(last.Counters.PrimaryRays+last.Counters.SecondaryRays+last.Counters.ShadowRays)):new([],0);
    public IReadOnlyList<WorkerContributionRow> WorkerRows=>Ranking.Rows;
    public string ContributionHeading=>(IsSessionActive?_session.Request?.Mode:_sessions.MostRecentSuccessful?.Mode)
        ==RenderMode.DistributedAllCore?"NODE CONTRIBUTIONS":"WORKER CONTRIBUTIONS";
    public string ContributionStatus=>(IsSessionActive && _session.Room is not null?"Current accepted work":"Last successful result")+
        $" · {Ranking.TotalRays.ToString(CultureInfo.InvariantCulture)} rays";
    public string LocalCapacityText=>"Available logical CPUs · up to 64 Workers · CPU/memory admission";
    public string TargetResolution=>Settings.ResolutionText;
    public string ResolutionNotice=>"All modes use the resolution saved in Settings.";
    public double? ProgressFraction=>_session.Progress?.Fraction;
    public double ProgressPercent=>(_session.Progress?.Fraction??0)*100;
    public string ProgressText=>_session.Progress is {} progress?$"{progress.CompletedSamples}/{progress.TargetSamples} samples":State==RenderState.Preparing?"Preparing":"—";
    public TimeSpan? Elapsed=>_session.Result?.ElapsedMilliseconds is {} value?TimeSpan.FromMilliseconds(value):null;
    public string ElapsedLabel=>_session.Request?.Mode==RenderMode.DistributedAllCore?"DISTRIBUTED ELAPSED":_session.Request?.Mode==RenderMode.LocalAllCore?"LOCAL ELAPSED":"SERIAL ELAPSED";
    public string ElapsedText=>Elapsed is {} value?$"{value.TotalSeconds:0.000} s":"—";
    public string RunNotice=>Preview==PreviewState.RealProvisional?"Accepted pixels · incomplete":"PBR · 1 SPP · depth 1 · seed 19";
    public PreviewState Preview=>_presentedRunId==_session.Request?.RunId && _presentedSequence>0?(_session.FrameIsProvisional || _presentedSequence!=_session.LatestFrame?.Sequence?PreviewState.RealProvisional:PreviewState.RealFinal):_preview;
    public bool IsPreviewUnavailable=>_preview==PreviewState.Unavailable && _presentedRunId is null;
    public string PreviewLabel=>_previewError is not null?"REAL PREVIEW · DECODE ERROR":Preview==PreviewState.RealProvisional?"REAL ACCEPTED PIXELS · INCOMPLETE":Preview==PreviewState.RealFinal?(_session.Request?.Mode==RenderMode.DistributedAllCore?"REAL CPU DISTRIBUTED FRAME":_session.Request?.Mode==RenderMode.LocalAllCore?"REAL CPU LOCAL PARALLEL FRAME":"REAL CPU SERIAL FRAME"):"STATIC REFERENCE PREVIEW";
    public string? PreviewError=>_previewError;
    public string? ErrorFeedback=>_lifecycleError??_previewError??(_session.Result?.Error is {} error?$"{error.Code}: {error.Message}":null);
    public string PreviewDescription=>Preview is PreviewState.RealFinal or PreviewState.RealProvisional?"Real Python ray-traced accepted pixels":"Static shogi room reference";
    public RenderFrame? LatestFrame=>_session.LatestFrame;
    public void ReportLifecycleError(string message) { _lifecycleError=message;OnPropertyChanged(nameof(ErrorFeedback)); }
    public void SetStaticPreviewAvailable(bool available) { _preview=available?PreviewState.StaticReference:PreviewState.Unavailable;NotifyAll(); }
    public void SetFramePresentation(Guid? runId,long sequence,string? error)
    {
        if(_disposed || runId!=_sessions.Snapshot.Request?.RunId)return;
        _previewError=error;
        if(error is null) { _presentedRunId=sequence==0?null:runId;_presentedSequence=sequence; }
        NotifyAll();
    }
    public Task InitializeAsync()=>_initialization??=InitializeCoreAsync();
    private async Task InitializeCoreAsync()
    {
        var info=_systemInfoProvider.GetSnapshotAsync(_lifetime.Token);
        var settings=await _settingsStore.LoadAsync(_lifetime.Token);
        _systemInformation=await info;_settings=settings.Settings;_settingsNotice=settings.Warning;
        await _sessions.RefreshAgentsAsync(_lifetime.Token);
        _initialized=true;NotifyAll();
    }
    private void OpenSettings()
    {
        if(_settingsDialog.Show(_settings,_settingsNotice) is not {IsValid:true} saved)return;
        _settings=saved;_settingsNotice=null;NotifyAll();
    }
    private bool CanStart()=>!_disposed && !_cacheBusy && !_remoteBusy && _initialized && _sessions.IsBackendAvailable && _sessions.CanStart && _sessions.IsPbrAvailable;
    private void Sessions_Changed(object? sender,EventArgs args)
    {
        if(_disposed || Interlocked.Exchange(ref _updateQueued,1)!=0)return;
        _dispatch(()=> { Interlocked.Exchange(ref _updateQueued,0);if(_disposed)return;_session=_sessions.Snapshot;NotifyAll(); });
    }
    private void NotifyAll()
    {
        NotifyRemote();
        NotifyPbr();
        OnPropertyChanged(nameof(WorkerRows));
        OnPropertyChanged(nameof(ContributionHeading));
        foreach(var property in new[]{nameof(Session),nameof(State),nameof(StatusText),nameof(Backend),nameof(BackendStatus),nameof(Local),nameof(Distributed),nameof(Contributions),nameof(ContributionStatus),nameof(ProgressFraction),nameof(ProgressPercent),nameof(ProgressText),nameof(Elapsed),nameof(ElapsedText),nameof(LatestFrame),nameof(IsLocalActive),nameof(LocalActionCommand),nameof(LocalActionText),nameof(LocalActionName),nameof(IsSessionActive),nameof(Preview),nameof(PreviewLabel),nameof(IsPreviewUnavailable),nameof(PreviewError),nameof(ErrorFeedback),nameof(PreviewDescription),nameof(RunNotice),nameof(TargetResolution),nameof(SystemInformation),nameof(Settings)})
            OnPropertyChanged(property);
        OnPropertyChanged(nameof(SerialScoreText));
        foreach(var name in new[]{nameof(SerialResultDetail),nameof(LocalResultDetail),nameof(DistributedResultDetail)})OnPropertyChanged(name);
        foreach(var name in new[]{nameof(EfficiencyText),nameof(ComparisonNotice)})OnPropertyChanged(name);
        foreach(var name in new[]{nameof(LocalScoreText),nameof(LocalCapacityText),nameof(ElapsedLabel),nameof(AllCoreActionCommand),nameof(AllCoreActionText),nameof(AllCoreActionName)}) OnPropertyChanged(name);
        StartAllCoreCommand.NotifyCanExecuteChanged();
        foreach(var name in new[]{nameof(DistributedActionCommand),nameof(DistributedActionText),nameof(DistributedActionName),nameof(DistributedScoreText),nameof(DistributedCapacityText),nameof(RemoteSystemsText),nameof(RemoteStatus),nameof(IsDistributedActive)})OnPropertyChanged(name);
        StartDistributedCommand.NotifyCanExecuteChanged();
        StartLocalCommand.NotifyCanExecuteChanged();CancelCommand.NotifyCanExecuteChanged();OpenSettingsCommand.NotifyCanExecuteChanged();
        ClearLocalCacheCommand.NotifyCanExecuteChanged();
    }
    public ValueTask DisposeAsync()=>new(_shutdown??=ShutdownCoreAsync());
    private async Task ShutdownCoreAsync()
    {
        _disposed=true;_sessions.Changed-=Sessions_Changed;_lifetime.Cancel();
        try { await (_initialization??Task.CompletedTask).WaitAsync(TimeSpan.FromSeconds(5)); }
        catch(OperationCanceledException) { }
        finally { try { await (_cacheOperation??Task.CompletedTask); } finally { _lifetime.Dispose(); } }
    }
}
