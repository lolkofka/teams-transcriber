// proclb: capture the audio rendered by ONE process (and its children) via WASAPI process loopback
// (Windows 10 2004+), write raw 16-bit PCM stereo to stdout.
//
//   proclb --name ms-teams [--rate 48000] [--exclude]
//   proclb --pid 1234
//
// stderr gets diagnostics; stdout is raw audio: int16 interleaved, 2 channels, --rate Hz.

using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;

namespace proclb
{
    [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceCompletionHandler
    {
        void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op);
    }

    [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceAsyncOperation
    {
        void GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object unk);
    }

    [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        [PreserveSig] int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, IntPtr format, IntPtr audioSessionGuid);
        [PreserveSig] int GetBufferSize(out uint numBufferFrames);
        [PreserveSig] int GetStreamLatency(out long latency);
        [PreserveSig] int GetCurrentPadding(out uint padding);
        [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closestMatch);
        [PreserveSig] int GetMixFormat(out IntPtr format);
        [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr eventHandle);
        [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        [PreserveSig] int GetBuffer(out IntPtr data, out uint numFrames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
        [PreserveSig] int ReleaseBuffer(uint numFrames);
        [PreserveSig] int GetNextPacketSize(out uint numFrames);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS
    {
        public uint TargetProcessId;
        public int ProcessLoopbackMode; // 0 = include target process tree, 1 = exclude
    }

    [StructLayout(LayoutKind.Sequential)]
    struct AUDIOCLIENT_ACTIVATION_PARAMS
    {
        public int ActivationType; // 1 = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        public AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS ProcessLoopbackParams;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROPVARIANT_BLOB
    {
        public ushort vt;
        public ushort r1, r2, r3;
        public uint cbSize;
        public IntPtr pBlobData;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    struct WAVEFORMATEX
    {
        public ushort wFormatTag, nChannels;
        public uint nSamplesPerSec, nAvgBytesPerSec;
        public ushort nBlockAlign, wBitsPerSample, cbSize;
    }

    [ComVisible(true)]
    class Handler : IActivateAudioInterfaceCompletionHandler
    {
        public readonly ManualResetEvent Done = new ManualResetEvent(false);
        public object Client;
        public int Hr;

        public void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op)
        {
            try { op.GetActivateResult(out Hr, out Client); }
            catch (Exception e) { Hr = e.HResult; }
            Done.Set();
        }
    }

    static class Program
    {
        [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = false)]
        static extern void ActivateAudioInterfaceAsync(
            [MarshalAs(UnmanagedType.LPWStr)] string deviceInterfacePath, ref Guid riid, IntPtr activationParams,
            IActivateAudioInterfaceCompletionHandler completionHandler, out IActivateAudioInterfaceAsyncOperation op);

        const string VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback";
        const uint AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000;
        const uint AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000;
        const uint AUDCLNT_BUFFERFLAGS_SILENT = 0x2;

        static int Main(string[] args)
        {
            int pid = 0; string name = null; uint rate = 48000; bool exclude = false;
            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i])
                {
                    case "--pid": pid = int.Parse(args[++i]); break;
                    case "--name": name = args[++i]; break;
                    case "--rate": rate = uint.Parse(args[++i]); break;
                    case "--exclude": exclude = true; break;
                }
            }
            if (pid == 0 && name != null)
            {
                var n = name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? name.Substring(0, name.Length - 4) : name;
                var procs = Process.GetProcessesByName(n);
                if (procs.Length == 0) { Console.Error.WriteLine($"proclb: no process named {name}"); return 2; }
                pid = procs.OrderBy(p => { try { return p.StartTime; } catch { return DateTime.MaxValue; } }).First().Id;
            }
            if (pid == 0) { Console.Error.WriteLine("proclb: --pid N or --name process.exe required"); return 2; }

            var ap = new AUDIOCLIENT_ACTIVATION_PARAMS
            {
                ActivationType = 1,
                ProcessLoopbackParams = new AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS { TargetProcessId = (uint)pid, ProcessLoopbackMode = exclude ? 1 : 0 }
            };
            int apSize = Marshal.SizeOf<AUDIOCLIENT_ACTIVATION_PARAMS>();
            IntPtr apPtr = Marshal.AllocHGlobal(apSize);
            Marshal.StructureToPtr(ap, apPtr, false);
            var pv = new PROPVARIANT_BLOB { vt = 0x41 /* VT_BLOB */, cbSize = (uint)apSize, pBlobData = apPtr };
            IntPtr pvPtr = Marshal.AllocHGlobal(Marshal.SizeOf<PROPVARIANT_BLOB>());
            Marshal.StructureToPtr(pv, pvPtr, false);

            var handler = new Handler();
            var iid = typeof(IAudioClient).GUID;
            ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ref iid, pvPtr, handler, out var op);
            handler.Done.WaitOne();
            if (handler.Hr != 0 || handler.Client == null) { Console.Error.WriteLine($"proclb: activation failed hr=0x{handler.Hr:X8}"); return 3; }
            var client = (IAudioClient)handler.Client;

            var wfx = new WAVEFORMATEX { wFormatTag = 1, nChannels = 2, nSamplesPerSec = rate, wBitsPerSample = 16, cbSize = 0 };
            wfx.nBlockAlign = (ushort)(wfx.nChannels * wfx.wBitsPerSample / 8);
            wfx.nAvgBytesPerSec = wfx.nSamplesPerSec * wfx.nBlockAlign;
            IntPtr wfxPtr = Marshal.AllocHGlobal(Marshal.SizeOf<WAVEFORMATEX>());
            Marshal.StructureToPtr(wfx, wfxPtr, false);

            int hr = client.Initialize(0, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK, 2000000, 0, wfxPtr, IntPtr.Zero);
            if (hr != 0) { Console.Error.WriteLine($"proclb: Initialize failed hr=0x{hr:X8}"); return 4; }

            var evt = new AutoResetEvent(false);
            hr = client.SetEventHandle(evt.SafeWaitHandle.DangerousGetHandle());
            if (hr != 0) { Console.Error.WriteLine($"proclb: SetEventHandle failed hr=0x{hr:X8}"); return 4; }

            var capIid = typeof(IAudioCaptureClient).GUID;
            hr = client.GetService(ref capIid, out var capObj);
            if (hr != 0) { Console.Error.WriteLine($"proclb: GetService failed hr=0x{hr:X8}"); return 4; }
            var cap = (IAudioCaptureClient)capObj;

            hr = client.Start();
            if (hr != 0) { Console.Error.WriteLine($"proclb: Start failed hr=0x{hr:X8}"); return 4; }
            Console.Error.WriteLine($"proclb: capturing pid {pid} ({(exclude ? "excluding" : "including")} process tree), {rate} Hz int16 stereo");

            var stdout = Console.OpenStandardOutput();
            var target = Process.GetProcessById(pid);
            byte[] buf = new byte[1 << 16];
            while (true)
            {
                if (!evt.WaitOne(1000))
                {
                    if (target.HasExited) { Console.Error.WriteLine("proclb: target exited"); break; }
                    continue;
                }
                while (true)
                {
                    hr = cap.GetNextPacketSize(out uint packet);
                    if (hr != 0 || packet == 0) break;
                    hr = cap.GetBuffer(out IntPtr data, out uint frames, out uint flags, out _, out _);
                    if (hr != 0) break;
                    int bytes = (int)(frames * wfx.nBlockAlign);
                    if (bytes > buf.Length) buf = new byte[bytes];
                    if ((flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0) Array.Clear(buf, 0, bytes);
                    else Marshal.Copy(data, buf, 0, bytes);
                    cap.ReleaseBuffer(frames);
                    try { stdout.Write(buf, 0, bytes); stdout.Flush(); }
                    catch (IOException) { client.Stop(); return 0; } // reader went away
                }
            }
            client.Stop();
            return 0;
        }
    }
}
