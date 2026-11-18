using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

public static class Foreground {
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("kernel32.dll")] static extern uint WTSGetActiveConsoleSessionId();

    static string[] Sample() {
        int session = Process.GetCurrentProcess().SessionId;
        if (session <= 0 || (uint)session != WTSGetActiveConsoleSessionId()) return null;
        IntPtr window = GetForegroundWindow();
        uint pid;
        if (window == IntPtr.Zero || GetWindowThreadProcessId(window, out pid) == 0 || pid == 0) return null;
        using (var p = Process.GetProcessById(checked((int)pid))) {
            string name = p.ProcessName;
            uint check;
            if (p.SessionId != session || p.HasExited || window != GetForegroundWindow() ||
                    GetWindowThreadProcessId(window, out check) == 0 || check != pid) return null;
            return new[] {session.ToString(CultureInfo.InvariantCulture), pid.ToString(CultureInfo.InvariantCulture), name};
        }
    }

    public static int Main() {
        string destination = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "sample");
        string[] sample = null;
        try { sample = Sample(); }
        catch (ArgumentException) {} // Window owner exited while being observed.
        catch (InvalidOperationException) {}
        catch (System.ComponentModel.Win32Exception) {} // Protected or inaccessible owner.
        try {
            File.WriteAllLines(destination + ".tmp", sample ?? new string[0], Encoding.UTF8);
            File.Move(destination + ".tmp", destination);
            return 0;
        } catch (IOException) { return 1; }
        catch (UnauthorizedAccessException) { return 1; }
    }
}
