using DistributedRayTrace.Contracts.Enums;

namespace DistributedRayTrace.Desktop.ViewModels;

public sealed partial class MainViewModel
{
    private bool SelectedDistributedAvailable=>_sessions.IsPbrDistributedAvailable;
    public double PreviewCanvasWidth=>_session.Request?.Width??Settings.Width;
    public double PreviewCanvasHeight=>_session.Request?.Height??Settings.Height;
    public string ViewportTitle=>"SHOGI ROOM · PBR";
    public string WorkloadDescription=>"Original room · camera 5 · 1 SPP · depth 1";
    public string PbrDiagnostics=>_session.Room?.Counters is {} c
        ?$"Accepted rays: {c.PrimaryRays+c.SecondaryRays+c.ShadowRays:N0}"
        :"Accepted samples form the progressive image";
    private Task StartSelectedAsync(RenderMode mode)=>_sessions.StartAsync(_settings,mode);
    private void NotifyPbr()
    {
        foreach(var p in new[]{nameof(PreviewCanvasWidth),nameof(PreviewCanvasHeight),
            nameof(ViewportTitle),nameof(WorkloadDescription),nameof(PbrDiagnostics)})OnPropertyChanged(p);
    }
}
