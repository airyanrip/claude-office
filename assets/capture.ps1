# Screen capture for Claude Office meeting reports.
#   One window:  powershell -ExecutionPolicy Bypass -File capture.ps1 -Out shot.png -Title "part of window title"
#   Full screen: powershell -ExecutionPolicy Bypass -File capture.ps1 -Out shot.png
# A window is captured with PrintWindow, so it works even when other windows cover it.
param([Parameter(Mandatory = $true)][string]$Out, [string]$Title = '')

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class CapW {
    public delegate bool EnumProc(IntPtr h, IntPtr p);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr p);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    public static IntPtr Find(string part) {
        IntPtr hit = IntPtr.Zero;
        EnumWindows((h, p) => {
            if (!IsWindowVisible(h)) return true;
            var sb = new StringBuilder(512);
            GetWindowText(h, sb, 512);
            if (sb.Length > 0 && sb.ToString().IndexOf(part, StringComparison.OrdinalIgnoreCase) >= 0) { hit = h; return false; }
            return true;
        }, IntPtr.Zero);
        return hit;
    }
}
'@
[void][CapW]::SetProcessDPIAware()
$dir = Split-Path -Parent ([IO.Path]::GetFullPath($Out))
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }

if ($Title) {
    $h = [CapW]::Find($Title)
    if ($h -eq [IntPtr]::Zero) { Write-Error "window not found: $Title"; exit 1 }
    if ([CapW]::IsIconic($h)) { [void][CapW]::ShowWindow($h, 9); Start-Sleep -Milliseconds 400 }
    $r = New-Object CapW+RECT
    [void][CapW]::GetWindowRect($h, [ref]$r)
    $w = $r.R - $r.L; $hh = $r.B - $r.T
    $bmp = New-Object Drawing.Bitmap $w, $hh
    $g = [Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    [void][CapW]::PrintWindow($h, $hdc, 2)
    $g.ReleaseHdc($hdc)
} else {
    $vs = [Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object Drawing.Bitmap $vs.Width, $vs.Height
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($vs.Left, $vs.Top, 0, 0, $bmp.Size)
}
$g.Dispose()
if (Test-Path $Out) { Remove-Item $Out -Force }
$bmp.Save([IO.Path]::GetFullPath($Out), [Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
"$Out"
