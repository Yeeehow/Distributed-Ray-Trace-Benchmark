using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;

namespace DistributedRayTrace.Desktop.Services.Rendering;

/// <summary>Launch suspended, assign a non-breakaway kill-on-close Job, then resume.
/// This captures venv launcher descendants before any user instruction can run.</summary>
public sealed class OwnedPythonProcess : IDisposable
{
    private IntPtr _job;
    private IntPtr _processHandle;
    private Process? _process;
    private int _disposed;
    private readonly AnonymousPipeServerStream _input = new(PipeDirection.Out, HandleInheritability.Inheritable);
    private readonly AnonymousPipeServerStream _output = new(PipeDirection.In, HandleInheritability.Inheritable);
    private readonly AnonymousPipeServerStream _error = new(PipeDirection.In, HandleInheritability.Inheritable);
    public StreamWriter Input { get; private set; } = null!;
    public StreamReader Output { get; private set; } = null!;
    public StreamReader Error { get; private set; } = null!;
    public int LauncherId => _process!.Id;
    public bool HasExited => WaitForSingleObject(_processHandle, 0) == 0;
    public int ExitCode
    {
        get { if (!HasExited) throw new InvalidOperationException("Process still running."); Check(GetExitCodeProcess(_processHandle, out var code)); return unchecked((int)code); }
    }

    public OwnedPythonProcess(string executable, IEnumerable<string> arguments, string workingDirectory)
    {
        IntPtr attributes = IntPtr.Zero, handles = IntPtr.Zero;
        PROCESS_INFORMATION info = default;
        try
        {
            _job = CreateJobObject(IntPtr.Zero, null);
            Check(_job != IntPtr.Zero);
            var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = 0x2000; // KILL_ON_JOB_CLOSE, no breakaway permission.
            Check(SetInformationJobObject(_job, 9, ref limits, (uint)Marshal.SizeOf(limits)));
            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            attributes = Marshal.AllocHGlobal(size);
            Check(InitializeProcThreadAttributeList(attributes, 1, 0, ref size));
            var inherited = new[] { _input.ClientSafePipeHandle.DangerousGetHandle(), _output.ClientSafePipeHandle.DangerousGetHandle(), _error.ClientSafePipeHandle.DangerousGetHandle() };
            handles = Marshal.AllocHGlobal(IntPtr.Size * inherited.Length);
            Marshal.Copy(inherited, 0, handles, inherited.Length);
            Check(UpdateProcThreadAttribute(attributes, 0, (IntPtr)0x20002, handles, (IntPtr)(IntPtr.Size * inherited.Length), IntPtr.Zero, IntPtr.Zero));
            var startup = new STARTUPINFOEX { lpAttributeList = attributes };
            startup.StartupInfo.cb = Marshal.SizeOf<STARTUPINFOEX>();
            startup.StartupInfo.dwFlags = 0x100;
            startup.StartupInfo.hStdInput = inherited[0]; startup.StartupInfo.hStdOutput = inherited[1]; startup.StartupInfo.hStdError = inherited[2];
            var command = new StringBuilder(Quote(executable) + " " + string.Join(" ", arguments.Select(Quote)));
            Check(CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, true, 0x08080004, IntPtr.Zero, workingDirectory, ref startup, out info));
            // Failure before ResumeThread terminates only the still-suspended owned child.
            if (!AssignProcessToJobObject(_job, info.hProcess))
            {
                var error = Marshal.GetLastWin32Error(); TerminateProcess(info.hProcess, 1);
                WaitForSingleObject(info.hProcess, 5000); throw new Win32Exception(error);
            }
            _process = Process.GetProcessById(info.dwProcessId);
            _processHandle = info.hProcess;
            info.hProcess = IntPtr.Zero; // Retain an authoritative handle through exit-code observation.
            _input.DisposeLocalCopyOfClientHandle(); _output.DisposeLocalCopyOfClientHandle(); _error.DisposeLocalCopyOfClientHandle();
            Input = new StreamWriter(_input, new UTF8Encoding(false), 1024, true) { AutoFlush = true };
            Output = new StreamReader(_output, new UTF8Encoding(false, true), false, 4096, true);
            Error = new StreamReader(_error, new UTF8Encoding(false, true), false, 1024, true);
            Check(ResumeThread(info.hThread) != uint.MaxValue);
        }
        catch { Dispose(); throw; }
        finally
        {
            if (info.hThread != IntPtr.Zero) CloseHandle(info.hThread);
            if (info.hProcess != IntPtr.Zero) CloseHandle(info.hProcess);
            if (attributes != IntPtr.Zero) { DeleteProcThreadAttributeList(attributes); Marshal.FreeHGlobal(attributes); }
            if (handles != IntPtr.Zero) Marshal.FreeHGlobal(handles);
        }
    }

    public int[] ProcessIds()
    {
        var memory = Marshal.AllocHGlobal(8 + 1024 * IntPtr.Size);
        try
        {
            Check(QueryInformationJobObject(_job, 3, memory, 8 + 1024 * IntPtr.Size, IntPtr.Zero));
            var count = Marshal.ReadInt32(memory, 4);
            if (count > 1024) throw new IOException("Owned process count exceeds safety bound.");
            return Enumerable.Range(0, count).Select(i => checked((int)Marshal.ReadIntPtr(memory, 8 + i * IntPtr.Size))).ToArray();
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    public bool Owns(int pid) => ProcessIds().Contains(pid);
    public async Task WaitForExitAsync(TimeSpan timeout)
    {
        var handle=_processHandle;
        var status=await Task.Run(()=>WaitForSingleObject(handle,checked((uint)timeout.TotalMilliseconds))).ConfigureAwait(false);
        if(status==258) throw new TimeoutException("Owned process exit deadline expired.");
        Check(status==0);
    }
    public async Task TerminateAndVerifyAsync()
    {
        Check(TerminateJobObject(_job, 137));
        await WaitForExitAsync(TimeSpan.FromSeconds(3)).ConfigureAwait(false);
        var clock = Stopwatch.StartNew();
        while (ProcessIds().Length != 0 && clock.Elapsed < TimeSpan.FromSeconds(3)) await Task.Delay(20).ConfigureAwait(false);
        if (ProcessIds().Length != 0) throw new IOException("Owned computation process termination could not be verified.");
    }

    public void Dispose()
    {
        if(Interlocked.Exchange(ref _disposed,1)!=0) return;
        if (_job != IntPtr.Zero) { CloseHandle(_job); _job = IntPtr.Zero; }
        // A crashed child can close stdin with a buffered shutdown write still
        // pending. Its broken-pipe flush must not skip the remaining handles.
        foreach (var resource in new IDisposable?[] { Input, Output, Error, _input, _output, _error, _process })
            try { resource?.Dispose(); } catch (Exception error) when (error is IOException or ObjectDisposedException) { }
        if (_processHandle != IntPtr.Zero) { CloseHandle(_processHandle); _processHandle=IntPtr.Zero; }
    }

    private static string Quote(string text)
    {
        var value=new StringBuilder("\"");int slashes=0;
        foreach(var character in text)
        {
            if(character=='\\') { slashes++;continue; }
            value.Append('\\',character=='\"' ? slashes*2+1 : slashes).Append(character);slashes=0;
        }
        return value.Append('\\',slashes*2).Append('"').ToString();
    }
    private static void Check(bool success) { if (!success) throw new Win32Exception(Marshal.GetLastWin32Error()); }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct STARTUPINFO { public int cb; public string? lpReserved, lpDesktop, lpTitle; public int dwX,dwY,dwXSize,dwYSize,dwXCountChars,dwYCountChars,dwFillAttribute,dwFlags; public short wShowWindow,cbReserved2; public IntPtr lpReserved2,hStdInput,hStdOutput,hStdError; }
    [StructLayout(LayoutKind.Sequential)] private struct STARTUPINFOEX { public STARTUPINFO StartupInfo; public IntPtr lpAttributeList; }
    [StructLayout(LayoutKind.Sequential)] private struct PROCESS_INFORMATION { public IntPtr hProcess,hThread; public int dwProcessId,dwThreadId; }
    [StructLayout(LayoutKind.Sequential)] private struct BASIC_LIMIT { public long PerProcessUserTimeLimit,PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinimumWorkingSetSize,MaximumWorkingSetSize; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass,SchedulingClass; }
    [StructLayout(LayoutKind.Sequential)] private struct IO_COUNTERS { public ulong ReadOperationCount,WriteOperationCount,OtherOperationCount,ReadTransferCount,WriteTransferCount,OtherTransferCount; }
    [StructLayout(LayoutKind.Sequential)] private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION { public BASIC_LIMIT BasicLimitInformation; public IO_COUNTERS IoInfo; public UIntPtr ProcessMemoryLimit,JobMemoryLimit,PeakProcessMemoryUsed,PeakJobMemoryUsed; }
    [DllImport("kernel32.dll",SetLastError=true,CharSet=CharSet.Unicode)] private static extern IntPtr CreateJobObject(IntPtr attributes,string? name);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool SetInformationJobObject(IntPtr job,int type,ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION data,uint size);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool QueryInformationJobObject(IntPtr job,int type,IntPtr data,int size,IntPtr result);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool GetExitCodeProcess(IntPtr process,out uint code);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] private static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,int flags,ref IntPtr size);
    [DllImport("kernel32.dll",SetLastError=true)] private static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr size,IntPtr previous,IntPtr returned);
    [DllImport("kernel32.dll")] private static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll",SetLastError=true,CharSet=CharSet.Unicode)] private static extern bool CreateProcess(string app,StringBuilder command,IntPtr processAttributes,IntPtr threadAttributes,bool inherit,uint flags,IntPtr environment,string directory,ref STARTUPINFOEX startup,out PROCESS_INFORMATION info);
}
