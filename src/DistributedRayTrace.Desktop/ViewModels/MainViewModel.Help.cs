using CommunityToolkit.Mvvm.Input;
using DistributedRayTrace.Desktop.Services.Cache;
using DistributedRayTrace.Desktop.Services.Dialogs;

namespace DistributedRayTrace.Desktop.ViewModels;

public sealed partial class MainViewModel
{
    public IRelayCommand OpenUserGuideCommand { get; private set; } = null!;
    public IRelayCommand OpenBenchmarkInformationCommand { get; private set; } = null!;
    public IRelayCommand OpenAboutCommand { get; private set; } = null!;
    public IAsyncRelayCommand ClearLocalCacheCommand { get; private set; } = null!;
    private ILocalRenderCache? _localCache;
    private ILocalCacheDialogs? _cacheDialogs;
    private bool _cacheBusy;
    private Task? _cacheOperation;

    private void InitializeHelp(Action<HelpTopic>? showHelp, ILocalRenderCache? cache, ILocalCacheDialogs? cacheDialogs)
    {
        OpenUserGuideCommand = new RelayCommand(() => showHelp?.Invoke(HelpTopic.UserGuide), () => !_disposed);
        OpenBenchmarkInformationCommand = new RelayCommand(() => showHelp?.Invoke(HelpTopic.BenchmarkInformation), () => !_disposed);
        OpenAboutCommand = new RelayCommand(() => showHelp?.Invoke(HelpTopic.About), () => !_disposed);
        _localCache = cache; _cacheDialogs = cacheDialogs;
        ClearLocalCacheCommand = new AsyncRelayCommand(() => _cacheOperation = ClearLocalCacheAsync(), CanClearLocalCache);
    }

    private bool CanClearLocalCache() => !_disposed && _initialized && !_cacheBusy && !_remoteBusy
        && !IsSessionActive && _sessions.CanStart && _localCache is not null && _cacheDialogs is not null;

    private async Task ClearLocalCacheAsync()
    {
        if (!CanClearLocalCache()) return;
        _cacheBusy = true; NotifyAll();
        try
        {
            if (!_cacheDialogs!.ConfirmClearLocalCache(_localCache!.DirectoryPath)) return;
            var result = await Task.Run(() => _localCache.Clear(_lifetime.Token), _lifetime.Token);
            if (!_disposed) _cacheDialogs.ShowLocalCacheResult(
                $"Local cache cleared. Deleted {result.FilesDeleted} files and {result.DirectoriesDeleted} folders.", true);
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { }
        catch (Exception error)
        {
            if (!_disposed) _cacheDialogs!.ShowLocalCacheResult("Cache cleanup stopped. Some files may remain.\n\n" + error.Message, false);
        }
        finally { _cacheBusy = false; if (!_disposed) NotifyAll(); }
    }
}
