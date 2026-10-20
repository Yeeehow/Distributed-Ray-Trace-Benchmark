using System.Windows.Media.Imaging;

namespace DistributedRayTrace.Desktop.Services.Preview;

public interface IReferencePreviewSource
{
    Task<BitmapSource> LoadAsync(CancellationToken cancellationToken = default);
}
