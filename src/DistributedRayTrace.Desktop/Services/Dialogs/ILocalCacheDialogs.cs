namespace DistributedRayTrace.Desktop.Services.Dialogs;

public interface ILocalCacheDialogs
{
    bool ConfirmClearLocalCache(string directory);
    void ShowLocalCacheResult(string message, bool success);
}
