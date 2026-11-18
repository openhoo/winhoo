# Read the physical console from a service session, without opening the input
# desktop or sending input. This backend only provisions Windows 11 ARM64.
function Get-WinHooConsole {
    if (-not ('WinHoo.ConsoleReader' -as [type])) {
        $null = Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace WinHoo {
    public sealed class ConsoleState {
        public uint? id;
        public string account, connection = "unknown", lockState = "unknown";
        public int? error;
    }
    public static class ConsoleReader {
        // https://learn.microsoft.com/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w
        // The Windows 7 reversed flags do not apply to this Windows 11 backend.
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct Level1 {
            public uint Id;
            public int State, Flags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=33)] public string Station;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=21)] public string User;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=18)] public string Domain;
            public long Logon, Connect, Disconnect, LastInput, Current;
            public uint InBytes, OutBytes, InFrames, OutFrames, InCompressed, OutCompressed;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct InfoEx { public uint Level; public Level1 Data; }
        [DllImport("kernel32.dll")] static extern uint WTSGetActiveConsoleSessionId();
        [DllImport("wtsapi32.dll", EntryPoint="WTSQuerySessionInformationW", SetLastError=true)]
        static extern bool Query(IntPtr server, uint session, int infoClass, out IntPtr data, out uint size);
        [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr data);

        public static ConsoleState Read() {
            var result = new ConsoleState();
            uint id = WTSGetActiveConsoleSessionId();
            if (id == UInt32.MaxValue || id == 0) return result;
            result.id = id;
            IntPtr data = IntPtr.Zero;
            uint size;
            try {
                if (!Query(IntPtr.Zero, id, 25 /* WTSSessionInfoEx */, out data, out size)) {
                    result.error = Marshal.GetLastWin32Error();
                    return result;
                }
                if (data == IntPtr.Zero || size < Marshal.SizeOf(typeof(InfoEx))) return result;
                var info = (InfoEx)Marshal.PtrToStructure(data, typeof(InfoEx));
                if (info.Level != 1 || info.Data.Id != id || WTSGetActiveConsoleSessionId() != id) return result;
                if (!String.IsNullOrEmpty(info.Data.User))
                    result.account = info.Data.Domain + "\\" + info.Data.User;
                result.connection = info.Data.State == 0 ? "active" : "inactive";
                if (info.Data.Flags == 0) result.lockState = "locked";
                if (info.Data.Flags == 1) result.lockState = "unlocked";
                return result;
            } finally {
                if (data != IntPtr.Zero) WTSFreeMemory(data);
            }
        }
    }
}
'@ -ErrorAction Stop
    }
    $s = [WinHoo.ConsoleReader]::Read()
    @{id=$s.id; account=$s.account; connection=$s.connection; lock=$s.lockState; error=$s.error}
}
