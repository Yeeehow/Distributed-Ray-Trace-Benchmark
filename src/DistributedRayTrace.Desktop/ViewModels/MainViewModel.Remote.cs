using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using DistributedRayTrace.Desktop.Models;

namespace DistributedRayTrace.Desktop.ViewModels;

public sealed class RemotePeerViewModel(RemotePeerState state,Action<string,bool> select):ObservableObject
{
    private bool _selected=state.Selected;
    public string Id=>state.Id;
    public string Endpoint=>$"{state.Host}:{state.Port}";
    public string Fingerprint=>state.Fingerprint;
    public string State=>state.State;
    public string Capability=>state.Compatible?"Verified · original room · Float64 · 160×90 / 640×360":"Awaiting authenticated capability verification";
    public bool Selected {get=>_selected;set {if(SetProperty(ref _selected,value))select(Id,value);}}
}

public sealed partial class MainViewModel
{
    private bool _remoteBusy;
    public IAsyncRelayCommand ConnectRemoteCommand {get;}
    public IAsyncRelayCommand DisconnectRemoteCommand {get;}
    public bool CanEditRemote=>!_disposed && !IsSessionActive && !_remoteBusy && !_cacheBusy;
    public IReadOnlyList<RemotePeerViewModel> RemotePeers=>_sessions.RemotePeers.Select(p=>new RemotePeerViewModel(p,SelectPeer)).ToArray();
    private bool CanManageRemote()=>CanEditRemote && _sessions.RemotePeers.Count>0;
    private void SelectPeer(string id,bool selected)
    {
        try {_sessions.SelectRoomPeer(id,selected);}
        catch(Exception e) {_lifecycleError=e.Message;NotifyAll();}
    }
    public Task PairAgentsFileAsync(string path)=>RemoteOperationAsync(()=>_sessions.ImportRoomAgentsAsync(path,_lifetime.Token));
    private async Task RemoteOperationAsync(Func<Task> action)
    {
        if(!CanEditRemote)return;
        _remoteBusy=true;NotifyAll();
        try {await action();_lifecycleError=null;}
        catch(OperationCanceledException) when(_lifetime.IsCancellationRequested) { }
        catch(Exception e) {_lifecycleError="Remote: "+e.Message;}
        finally {_remoteBusy=false;NotifyAll();}
    }
    private void NotifyRemote()
    {
        OnPropertyChanged(nameof(RemotePeers));OnPropertyChanged(nameof(CanEditRemote));
        ConnectRemoteCommand.NotifyCanExecuteChanged();DisconnectRemoteCommand.NotifyCanExecuteChanged();
    }
}
