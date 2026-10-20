using System.Windows;
using DistributedRayTrace.Desktop.ViewModels;

namespace DistributedRayTrace.Desktop.Views;

public partial class RemoteWindow:Window
{
    private readonly MainViewModel _viewModel;
    public RemoteWindow(MainViewModel viewModel)
    {
        InitializeComponent();_viewModel=viewModel;DataContext=viewModel;
    }
    private async void PairAgents_Click(object sender,RoutedEventArgs e)
    {
        var dialog=new Microsoft.Win32.OpenFileDialog {Title="Import trusted pinned Agent topology",Filter="Agent topology (*.json)|*.json",CheckFileExists=true};
        if(dialog.ShowDialog(this)==true)await _viewModel.PairAgentsFileAsync(dialog.FileName);
    }
}
