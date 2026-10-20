using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using DistributedRayTrace.Desktop.Models;
using DistributedRayTrace.Desktop.Services.Settings;

namespace DistributedRayTrace.Desktop.ViewModels;

/// <summary>Selection, validation and persistence. The view alone decides how to dismiss its dialog.</summary>
public sealed class SettingsViewModel : ObservableObject
{
    private readonly ISettingsStore _store;
    private ResolutionPreset? _selectedPreset;
    private string _feedback;
    private bool _isSaving;

    public SettingsViewModel(ISettingsStore store, ApplicationSettings current, string? notice = null)
    {
        _store = store;
        _selectedPreset = ResolutionPreset.Find(current.Width, current.Height);
        _feedback = notice ?? "";
        SaveCommand = new AsyncRelayCommand(SaveAsync, () => !IsSaving && IsValid);
    }

    public IReadOnlyList<ResolutionPreset> Presets => ResolutionPreset.All;
    public IAsyncRelayCommand SaveCommand { get; }
    public ApplicationSettings? SavedSettings { get; private set; }
    public event EventHandler? Saved;
    public ResolutionPreset? SelectedPreset
    {
        get => _selectedPreset;
        set
        {
            if (IsSaving || !SetProperty(ref _selectedPreset, value)) return;
            OnPropertyChanged(nameof(IsValid));
            SaveCommand.NotifyCanExecuteChanged();
        }
    }
    public bool IsValid => SelectedPreset is { } preset && ResolutionPreset.Find(preset.Width, preset.Height) == preset;
    public bool IsSaving => _isSaving;
    public bool CanDismiss => !IsSaving;
    public string Feedback => _feedback;

    private async Task SaveAsync()
    {
        if (IsSaving) return;
        if (!IsValid) { SetProperty(ref _feedback, "Select a supported resolution preset.", nameof(Feedback)); return; }
        var preset = SelectedPreset!;
        var candidate = new ApplicationSettings { Width = preset.Width, Height = preset.Height };
        SetSaving(true);
        SetProperty(ref _feedback, "Saving…", nameof(Feedback));
        SettingsSaveResult result;
        try { result = await _store.SaveAsync(candidate); }
        catch (OperationCanceledException) { result = new(false, "Settings save was cancelled. Your previous resolution is unchanged."); }
        catch (Exception) { result = new(false, "Settings could not be saved. Your previous resolution is unchanged."); }
        SetSaving(false);
        if (!result.Success)
        {
            SetProperty(ref _feedback, result.Error ?? "Settings could not be saved.", nameof(Feedback));
            return;
        }
        SavedSettings = candidate;
        Saved?.Invoke(this, EventArgs.Empty);
    }

    private void SetSaving(bool value)
    {
        SetProperty(ref _isSaving, value, nameof(IsSaving));
        OnPropertyChanged(nameof(CanDismiss));
        SaveCommand.NotifyCanExecuteChanged();
    }
}
