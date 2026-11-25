using System;
using System.Runtime.InteropServices;
using System.Text;

internal static class RunHiddenProbe
{
    [DllImport("kernel32.dll")]
    private static extern IntPtr GetConsoleWindow();

    public static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.WriteLine("console=" + GetConsoleWindow().ToInt64());
        foreach (string argument in args)
            Console.WriteLine("argument=" + Convert.ToBase64String(Encoding.UTF8.GetBytes(argument)));
        Console.Error.WriteLine("stderr=Grüße – 日本語");
        if (Array.IndexOf(args, "--flood") >= 0)
            for (int i = 0; i < 8192; i++)
            {
                Console.Out.WriteLine("out-012345678901234567890123456789012345678901234567890123456789");
                Console.Error.WriteLine("err-012345678901234567890123456789012345678901234567890123456789");
            }
        return 37;
    }
}
