using CommunityToolkit.Mvvm.ComponentModel;
using System.IO;
using System.Text.RegularExpressions;
namespace DistributedRayTrace.Desktop.ViewModels;

public enum HelpTopic { UserGuide, BenchmarkInformation, About }
public sealed record HelpParagraph(string Heading,string Body);
public sealed record HelpPage(HelpTopic Topic,string Title,IReadOnlyList<HelpParagraph> Paragraphs);
public sealed class HelpViewModel : ObservableObject
{
    public static string FrontendVersion=>typeof(HelpViewModel).Assembly.GetName().Version!.ToString(3);
    public string Identity=>$"Distributed Ray Trace {FrontendVersion}";
    public IReadOnlyList<HelpPage> Pages {get;} =
    [
        new(HelpTopic.UserGuide,"User Guide",LoadUserGuide()),
        new(HelpTopic.BenchmarkInformation,"Benchmark Information",
        [
            new("Timing","Successful results store one contiguous backend execution interval including source preparation, numerical computation, central acceptance, finalization and confirmed cleanup. Initial IPC launch, final publication, frame transfer and WPF painting are excluded consistently across modes."),
            new("Relative performance","Local is the 100% baseline. For matching successful Local and Distributed workloads, Distributed shows 100 × Local time / Distributed time − 100, rounded to the nearest integer with midpoint ties away from zero. Matching Float64 output is accepted; cross-platform Float64 differences also compare when final RGB8 output and ray counters are identical. Different workloads, outputs or timing policies suppress comparison."),
            new("Workload and score","The fixed Shogi room uses CPU physically based path tracing at 160 × 90 or 640 × 360, with 1 sample per pixel and depth 1. The score is completed accepted samples divided by backend execution seconds, rounded down to an integer. Higher scores mean higher sample throughput for equivalent workloads; different resolutions or quality settings are not directly comparable."),
            new("Contribution and capacity","Serial and Local show accepted primary, continuation and shadow rays for each logical software Worker. Distributed groups accepted rays by machine, including Local. Available logical CPUs are admitted subject to memory and affinity, up to 64 Workers per machine and 256 total across Local and at most 16 remote nodes. This comparison is elapsed-time improvement, not processor-normalized efficiency or a scaling certification."),
            new("Numerical ownership","All modes use the same Float64 task definition and central task acceptance. The original source geometry and contact rules remain enforced. Numerical errors are surfaced; no contact bypass or numerical-success retry is used.")
        ]),
        new(HelpTopic.About,"About",
        [
            new("Distributed Ray Trace",$"Version {FrontendVersion}\nCPU path-tracing benchmark with local and distributed rendering support."),
            new("Platform and rendering","Windows desktop application. Physically based CPU path tracing with authenticated remote compute nodes. The separate Linux backend provides remote computation.")
        ])
    ];
    private static IReadOnlyList<HelpParagraph> LoadUserGuide()
    {
        using var resource=typeof(HelpViewModel).Assembly.GetManifestResourceStream("DistributedRayTrace.UserGuide.md")
            ?? throw new InvalidOperationException("Embedded user guide is missing.");
        using var reader=new StreamReader(resource);
        var text=reader.ReadToEnd().Replace("\r\n","\n");
        var sections=Regex.Matches(text,@"^### (.+)$",RegexOptions.Multiline);
        return sections.Select((section,index)=>new HelpParagraph(section.Groups[1].Value.Trim(),
            Regex.Replace(text[(section.Index+section.Length)..(index+1<sections.Count?sections[index+1].Index:text.Length)],
                @"^```[^\n]*\n?","",RegexOptions.Multiline).Trim())).ToArray();
    }
    private HelpPage _selectedPage;
    public HelpPage SelectedPage {get=>_selectedPage;set=>SetProperty(ref _selectedPage,value);}
    public HelpViewModel(HelpTopic topic)=>_selectedPage=Pages.Single(p=>p.Topic==topic);
}
