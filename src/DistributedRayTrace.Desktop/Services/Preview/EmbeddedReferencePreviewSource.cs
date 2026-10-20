using System.IO;
using System.Resources;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace DistributedRayTrace.Desktop.Services.Preview;

public sealed class EmbeddedReferencePreviewSource : IReferencePreviewSource
{
    private static readonly Lazy<BitmapSource> Reference = new(LoadEmbedded);
    public Task<BitmapSource> LoadAsync(CancellationToken cancellationToken = default) => Task.Run(() =>
    {
        cancellationToken.ThrowIfCancellationRequested();
        var image = Reference.Value;
        cancellationToken.ThrowIfCancellationRequested();
        return image;
    }, cancellationToken);

    private static BitmapSource LoadEmbedded()
    {
        // Read the existing WPF resource, independent of cwd, install location or Application.Current.
        using var resources = typeof(EmbeddedReferencePreviewSource).Assembly.GetManifestResourceStream("DistributedRayTrace.Desktop.g.resources")
            ?? throw new IOException("Embedded reference resources are unavailable.");
        using var reader = new ResourceSet(resources);
        using var stream = reader.GetObject("assets/preview_blend_with_glb.png") as Stream
            ?? throw new IOException("Embedded reference image is unavailable.");
        var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnLoad);
        var converted = new FormatConvertedBitmap(decoder.Frames[0], PixelFormats.Bgr32, null, 0);
        var stride = checked(converted.PixelWidth * 4);
        var pixels = new byte[checked(stride * converted.PixelHeight)];
        converted.CopyPixels(pixels, stride, 0);
        // Detach from the thread-owned WIC decoder before sharing/resampling on other threads.
        var bitmap = BitmapSource.Create(converted.PixelWidth, converted.PixelHeight, 96, 96, PixelFormats.Bgr32, null, pixels, stride);
        bitmap.Freeze();
        return bitmap;
    }
}
