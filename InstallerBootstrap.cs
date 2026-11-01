// Small Windows executable that unpacks the versioned source payload and
// starts the PowerShell setup window. Install-Backend.ps1 does the installation.
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;

internal static class InstallerBootstrap {
    [STAThread]
    private static void Main() {
        try {
            string folder = Path.Combine(Path.GetTempPath(), "CaleAISetup-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(folder);
            string archive = Path.Combine(folder, "package.zip");
            using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream("CaleAI.Bundle.zip")) {
                if (input == null) throw new InvalidOperationException("Installer package is missing.");
                using (FileStream output = File.Create(archive)) input.CopyTo(output);
            }
            ZipFile.ExtractToDirectory(archive, folder);
            string setup = Path.Combine(folder, "Setup.ps1");
            if (!File.Exists(setup)) throw new FileNotFoundException("Setup.ps1 is missing.");
            ProcessStartInfo start = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -STA -File \"" + setup + "\"");
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            Process.Start(start);
        } catch (Exception error) {
            MessageBox.Show(error.Message, "Cale AI Workbench installer", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
