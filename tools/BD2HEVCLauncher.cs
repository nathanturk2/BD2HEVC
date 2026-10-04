using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class BD2HEVCLauncher
{
    [STAThread]
    private static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string entry = Path.Combine(root, "bd2hevc.py");
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = "pythonw.exe",
                Arguments = "\"" + entry + "\" gui",
                WorkingDirectory = root,
                UseShellExecute = true,
            });
        }
        catch (Exception error)
        {
            MessageBox.Show(
                "BD2HEVC could not start Python. Ensure pythonw.exe is available.\n\n" + error.Message,
                "BD2HEVC",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }
}
