using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// Run only on the dedicated, unlocked test console. Never click client content.
public static class FocusCaption {
    [StructLayout(LayoutKind.Sequential)] struct Point { public int X, Y; public Point(int x, int y) { X=x; Y=y; } }
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct MouseInput { public int X,Y; public uint Data, Flags, Time; public UIntPtr Extra; }
    [StructLayout(LayoutKind.Sequential)] struct Input { public uint Type; public MouseInput Mouse; }
    [DllImport("kernel32.dll")] static extern uint WTSGetActiveConsoleSessionId();
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out Rect rect);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(Point pt);
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr h, uint flag);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out Point pt);
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] static extern uint SendInput(uint count, Input[] inputs, int size);

    static bool Owned(IntPtr window, int pid) {
        uint found;
        return window != IntPtr.Zero && GetWindowThreadProcessId(window, out found) != 0 && found == (uint)pid;
    }
    static bool Caption(IntPtr window, int pid, Point p) {
        if (!Owned(window,pid) || GetAncestor(WindowFromPoint(p),2) != window ||
            p.X < short.MinValue || p.X > short.MaxValue || p.Y < short.MinValue || p.Y > short.MaxValue) return false;
        UIntPtr hit;
        var xy = new IntPtr(unchecked((p.Y << 16) | (p.X & 65535)));
        return SendMessageTimeout(window,0x84,IntPtr.Zero,xy,2,100,out hit) != IntPtr.Zero && hit.ToUInt64()==2; // WM_NCHITTEST: HTCAPTION
    }
    static string Activate(Process target) {
        int pid=target.Id;
        if (Owned(GetForegroundWindow(),pid)) return "already-active";
        IntPtr window=target.MainWindowHandle;
        Rect r; Point old;
        if (!Owned(window,pid) || IsIconic(window) || !GetWindowRect(window,out r) || !GetCursorPos(out old)) return null;
        foreach (int y in new[]{20,12,28,36}) foreach (int pc in new[]{65,55,45,75,35,25}) {
            var p=new Point(r.Left+(r.Right-r.Left)*pc/100,r.Top+y);
            if (!Caption(window,pid,p)) continue;
            if (!SetCursorPos(p.X,p.Y)) return null;
            try {
                Point at;
                if (!GetCursorPos(out at) || at.X!=p.X || at.Y!=p.Y || !Caption(window,pid,p) || target.HasExited) return null;
                var click=new[]{new Input{Mouse=new MouseInput{Flags=2}},new Input{Mouse=new MouseInput{Flags=4}}};
                if (SendInput(2,click,Marshal.SizeOf(typeof(Input)))!=2) return null;
                for (int n=0;n<30;n++) {
                    if (Owned(GetForegroundWindow(),pid) && !target.HasExited) return "caption-click";
                    Thread.Sleep(30);
                }
                return null;
            } finally {
                Point at;
                if (GetCursorPos(out at) && at.X==p.X && at.Y==p.Y) SetCursorPos(old.X,old.Y);
            }
        }
        return null;
    }
    public static int Main(string[] args) {
        string file=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"sample");
        string[] rows=new string[0];
        try {
            int pid; long ticks;
            int session=Process.GetCurrentProcess().SessionId;
            if (args.Length==2 && int.TryParse(args[0],out pid) && pid>0 && long.TryParse(args[1],out ticks) &&
                session>0 && session==(int)WTSGetActiveConsoleSessionId() && SetProcessDpiAwarenessContext(new IntPtr(-4))) {
                using (var p=Process.GetProcessById(pid)) {
                    if (p.SessionId==session && p.StartTime.ToUniversalTime().Ticks==ticks && !p.HasExited) {
                        string method=Activate(p);
                        if (method!=null && Owned(GetForegroundWindow(),pid) && !p.HasExited)
                            rows=new[]{session.ToString(CultureInfo.InvariantCulture),pid.ToString(CultureInfo.InvariantCulture),p.ProcessName,method};
                    }
                }
            }
        } catch (ArgumentException) {} catch (InvalidOperationException) {} catch (System.ComponentModel.Win32Exception) {}
        File.WriteAllLines(file+".tmp",rows,Encoding.UTF8);
        File.Move(file+".tmp",file);
        return rows.Length==4?0:1;
    }
}
