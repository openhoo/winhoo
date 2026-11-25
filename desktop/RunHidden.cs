using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

// GUI subsystem entry point: no console is allocated before Main runs.
// Child processes receive real redirected handles and CREATE_NO_WINDOW.
internal static class RunHidden
{
    private static void Kill(Process child)
    {
        try { if (!child.HasExited) child.Kill(); } catch { }
    }

    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            slashes = 0;
            result.Append(character);
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    private static void Receipt(string path, string status, int? pid, int? exitCode)
    {
        if (path == null) return;
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        string json = "{\"schema\":\"winhoo.process/1\",\"status\":\"" + status
            + "\",\"pid\":" + (pid.HasValue ? pid.Value.ToString() : "null")
            + ",\"exitcode\":" + (exitCode.HasValue ? exitCode.Value.ToString() : "null")
            + ",\"generatedUtc\":\"" + DateTime.UtcNow.ToString("o") + "\"}\n";
        File.WriteAllText(temporary, json, new UTF8Encoding(false));
        if (File.Exists(path)) File.Replace(temporary, path, null);
        else File.Move(temporary, path);
    }

    private static string OutputPath(string value)
    {
        if (!Path.IsPathRooted(value) || Path.GetFullPath(value) != value)
            throw new ArgumentException("Output paths must be absolute");
        Directory.CreateDirectory(Path.GetDirectoryName(value));
        return value;
    }

    public static int Main(string[] args)
    {
        string stdout = null, stderr = null, status = null;
        try
        {
            int index = 0;
            while (index < args.Length && args[index] != "--")
            {
                string option = args[index++];
                if (index >= args.Length) throw new ArgumentException("Missing output path");
                string path = OutputPath(args[index++]);
                if (option == "--stdout") stdout = path;
                else if (option == "--stderr") stderr = path;
                else if (option == "--status") status = path;
                else throw new ArgumentException("Unsupported option");
            }
            if (++index >= args.Length) throw new ArgumentException("Expected -- and an executable");
            string executable = args[index++];
            if (!Path.IsPathRooted(executable) || Path.GetFullPath(executable) != executable)
                throw new ArgumentException("Executable must have an absolute path");
            if ((stdout != null && String.Equals(stdout, stderr, StringComparison.OrdinalIgnoreCase)) ||
                (status != null && (String.Equals(status, stdout, StringComparison.OrdinalIgnoreCase) || String.Equals(status, stderr, StringComparison.OrdinalIgnoreCase))))
                throw new ArgumentException("Output paths must be distinct");
            var command = new StringBuilder();
            while (index < args.Length)
            {
                if (command.Length > 0) command.Append(' ');
                command.Append(Quote(args[index++]));
            }
            Receipt(status, "starting", null, null);
            using (var output = stdout == null ? Stream.Null : new FileStream(stdout, FileMode.Create, FileAccess.Write, FileShare.ReadWrite))
            using (var error = stderr == null ? Stream.Null : new FileStream(stderr, FileMode.Create, FileAccess.Write, FileShare.ReadWrite))
            using (var child = Process.Start(new ProcessStartInfo(executable, command.ToString()) {
                UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
            }))
            {
                try
                {
                    child.StandardInput.Close();
                    Receipt(status, "running", child.Id, null);
                    Exception outputError = null, errorError = null;
                    var outputThread = new Thread(() => { try { child.StandardOutput.BaseStream.CopyTo(output); } catch (Exception e) { outputError = e; Kill(child); } });
                    var errorThread = new Thread(() => { try { child.StandardError.BaseStream.CopyTo(error); } catch (Exception e) { errorError = e; Kill(child); } });
                    outputThread.Start(); errorThread.Start();
                    child.WaitForExit(); outputThread.Join(); errorThread.Join();
                    if (outputError != null) throw outputError;
                    if (errorError != null) throw errorError;
                    Receipt(status, "exited", child.Id, child.ExitCode);
                    return child.ExitCode;
                }
                catch { Kill(child); throw; }
            }
        }
        catch (Exception exception)
        {
            try { if (stderr != null) File.AppendAllText(stderr, exception.ToString(), new UTF8Encoding(false)); } catch { }
            try { Receipt(status, "failed", null, 125); } catch { }
            return 125;
        }
    }
}
