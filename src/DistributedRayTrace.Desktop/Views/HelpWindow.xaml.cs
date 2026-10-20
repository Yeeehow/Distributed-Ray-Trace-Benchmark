using System.Windows;
using DistributedRayTrace.Desktop.ViewModels;

namespace DistributedRayTrace.Desktop.Views;

public partial class HelpWindow : Window
{
    public HelpWindow(HelpTopic topic)
    {
        InitializeComponent();
        DataContext = new HelpViewModel(topic);
    }
}
