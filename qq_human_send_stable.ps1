param(
  [ValidateSet("probe", "send", "calibrate")]
  [string]$Mode = "probe",
  [int]$GroupRow = 1,
  [int]$MemberRow = 3,
  [int]$GroupBaseY = 150,
  [int]$MemberBaseY = 322,
  [int]$SearchResultBaseY = 322,
  [string]$TargetQQ = "",
  [ValidateSet("text", "image")]
  [string]$MessageKind = "text",
  [string]$Message = "",
  [string]$ImagePath = "",
  [int]$WaitSeconds = 8,
  [string]$OutDir = "",
  [string]$TraceFile = "",
  [string]$CalibrationFile = ""
)

$ErrorActionPreference = "Stop"
if (-not $OutDir) {
  $OutDir = Join-Path $PSScriptRoot ("stable-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if (-not $TraceFile -and $env:NMF_TRACE_FILE) {
  $TraceFile = $env:NMF_TRACE_FILE
}

function Write-TraceStage([string]$Stage) {
  if (-not $TraceFile) { return }
  try {
    ((Get-Date).ToString("o") + " " + $Stage) | Add-Content -LiteralPath $TraceFile -Encoding UTF8
  } catch {}
}

Write-TraceStage "startup"

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$script:UiaAvailable = $false

Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class NmfStable {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, UIntPtr wParam, UIntPtr lParam);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out RECT pvAttribute, int cbAttribute);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT point);
  [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
}
"@

[NmfStable]::SetProcessDPIAware() | Out-Null

$script:ProfileTitle = -join ([char[]](0x8d44, 0x6599, 0x5361))
$script:NoticeTitle = -join ([char[]](0x7fa4, 0x516c, 0x544a))
$script:LastGroupLikeShot = ""
if ($MessageKind -eq "text" -and -not $Message) {
  $Message = -join ([char[]](0x6b22, 0x8fce, 0x8fdb, 0x7fa4))
}
if ($MessageKind -eq "image" -and -not $ImagePath) {
  throw "warmup image path is empty"
}

function Get-Text([IntPtr]$Hwnd) {
  $sb = New-Object System.Text.StringBuilder 512
  [NmfStable]::GetWindowText($Hwnd, $sb, $sb.Capacity) | Out-Null
  $sb.ToString()
}

function Get-Class([IntPtr]$Hwnd) {
  $sb = New-Object System.Text.StringBuilder 256
  [NmfStable]::GetClassName($Hwnd, $sb, $sb.Capacity) | Out-Null
  $sb.ToString()
}

function Get-Frame([IntPtr]$Hwnd) {
  $r = New-Object NmfStable+RECT
  $hr = [NmfStable]::DwmGetWindowAttribute($Hwnd, 9, [ref]$r, [Runtime.InteropServices.Marshal]::SizeOf([type][NmfStable+RECT]))
  if ($hr -ne 0 -or $r.Right -le $r.Left -or $r.Bottom -le $r.Top) {
    [NmfStable]::GetWindowRect($Hwnd, [ref]$r) | Out-Null
  }
  [pscustomobject]@{
    Left = [int]$r.Left
    Top = [int]$r.Top
    Right = [int]$r.Right
    Bottom = [int]$r.Bottom
    Width = [int]($r.Right - $r.Left)
    Height = [int]($r.Bottom - $r.Top)
  }
}

function Find-QQWindows {
  $qqPids = @(Get-Process QQ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
  $rows = New-Object System.Collections.ArrayList
  $cb = [NmfStable+EnumWindowsProc]{
    param([IntPtr]$hWnd, [IntPtr]$lParam)
    $procId = [uint32]0
    [NmfStable]::GetWindowThreadProcessId($hWnd, [ref]$procId) | Out-Null
    if ($qqPids -contains [int]$procId) {
      $frame = Get-Frame $hWnd
      $className = Get-Class $hWnd
      $title = Get-Text $hWnd
      if ($className -eq "Chrome_WidgetWin_1" -and $frame.Width -gt 20 -and $frame.Height -gt 20) {
        [void]$rows.Add([pscustomobject]@{
          Handle = $hWnd
          HandleValue = $hWnd.ToInt64()
          Title = $title
          Class = $className
          Visible = [NmfStable]::IsWindowVisible($hWnd)
          Left = $frame.Left
          Top = $frame.Top
          Width = $frame.Width
          Height = $frame.Height
          Area = $frame.Width * $frame.Height
        })
      }
    }
    return $true
  }
  [NmfStable]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  @($rows | Sort-Object Area -Descending)
}

function Get-MainQQWindow {
  $windows = @(Find-QQWindows)
  $visibleWindows = @($windows | Where-Object { $_.Visible })
  $main = $visibleWindows | Where-Object { $_.Title -eq "QQ" -and $_.Width -ge 800 -and $_.Height -ge 600 } | Sort-Object Area -Descending | Select-Object -First 1
  if (-not $main) {
    $main = $visibleWindows | Where-Object { $_.Width -ge 800 -and $_.Height -ge 600 } | Sort-Object Area -Descending | Select-Object -First 1
  }
  if (-not $main) {
    $main = $windows | Where-Object { $_.Title -eq "QQ" -and $_.Width -ge 800 -and $_.Height -ge 600 } | Sort-Object Area -Descending | Select-Object -First 1
  }
  if (-not $main) {
    $main = $windows | Where-Object { $_.Width -ge 800 -and $_.Height -ge 600 } | Sort-Object Area -Descending | Select-Object -First 1
  }
  if (-not $main) {
    throw "main QQ window not found"
  }
  $main
}

function Focus-Maximized([IntPtr]$Hwnd) {
  [NmfStable]::ShowWindowAsync($Hwnd, 3) | Out-Null
  Start-Sleep -Milliseconds 350
  [NmfStable]::SetWindowPos($Hwnd, [IntPtr](-1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
  Start-Sleep -Milliseconds 80
  [NmfStable]::SetWindowPos($Hwnd, [IntPtr](-2), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) | Out-Null
  [NmfStable]::BringWindowToTop($Hwnd) | Out-Null
  [NmfStable]::SetForegroundWindow($Hwnd) | Out-Null
  Start-Sleep -Milliseconds 450
}

function Click-At([int]$X, [int]$Y) {
  [NmfStable]::SetCursorPos($X, $Y) | Out-Null
  Start-Sleep -Milliseconds 100
  [NmfStable]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 70
  [NmfStable]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 350
}

function Click-AtFast([int]$X, [int]$Y) {
  [NmfStable]::SetCursorPos($X, $Y) | Out-Null
  Start-Sleep -Milliseconds 45
  [NmfStable]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 35
  [NmfStable]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 90
}

function RightClick-At([int]$X, [int]$Y) {
  [NmfStable]::SetCursorPos($X, $Y) | Out-Null
  Start-Sleep -Milliseconds 100
  [NmfStable]::mouse_event(8, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 70
  [NmfStable]::mouse_event(16, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 450
}

function DoubleClick-At([int]$X, [int]$Y) {
  [NmfStable]::SetCursorPos($X, $Y) | Out-Null
  Start-Sleep -Milliseconds 100
  for ($i = 0; $i -lt 2; $i++) {
    [NmfStable]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 45
    [NmfStable]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 90
  }
  Start-Sleep -Milliseconds 500
}

function Press-CtrlA-Backspace {
  [NmfStable]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x41, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x41, 0, 2, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 80
  [NmfStable]::keybd_event(0x08, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x08, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 180
}

function Set-ClipboardTextSafe([string]$Text) {
  $lastError = $null
  for ($i = 0; $i -lt 20; $i++) {
    try {
      [System.Windows.Forms.Clipboard]::SetDataObject($Text, $true, 10, 120)
      return $true
    } catch {
      $lastError = $_.Exception.Message
    }
    try {
      [System.Windows.Forms.Clipboard]::SetText($Text)
      return $true
    } catch {
      $lastError = $_.Exception.Message
    }
    Start-Sleep -Milliseconds (120 + ($i * 30))
  }
  throw ("clipboard write failed after retries: " + $lastError)
}

function Type-Digits([string]$Text) {
  if ($Text -notmatch '^[0-9]+$') {
    return $false
  }
  foreach ($ch in $Text.ToCharArray()) {
    $vk = 0x30 + [int]([string]$ch)
    [NmfStable]::keybd_event([byte]$vk, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 35
    [NmfStable]::keybd_event([byte]$vk, 0, 2, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 45
  }
  Start-Sleep -Milliseconds 260
  return $true
}

function Paste-Text([string]$Text) {
  try {
    [void](Set-ClipboardTextSafe $Text)
  } catch {
    if (Type-Digits $Text) {
      return
    }
    throw
  }
  Start-Sleep -Milliseconds 120
  [NmfStable]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x56, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x56, 0, 2, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 300
}

function Set-ClipboardImageSafe([string]$Path) {
  if (-not $Path) {
    throw "warmup image path is empty"
  }
  $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
  if (-not (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
    throw ("warmup image file not found: " + $Path)
  }

  $lastError = $null
  for ($i = 0; $i -lt 20; $i++) {
    $loaded = $null
    $bitmap = $null
    try {
      $loaded = [System.Drawing.Image]::FromFile($resolved.Path)
      $bitmap = New-Object System.Drawing.Bitmap $loaded
      [System.Windows.Forms.Clipboard]::SetDataObject($bitmap, $true, 10, 120)
      return $true
    } catch {
      $lastError = $_.Exception.Message
    } finally {
      if ($bitmap) { $bitmap.Dispose() }
      if ($loaded) { $loaded.Dispose() }
    }
    Start-Sleep -Milliseconds (160 + ($i * 40))
  }
  throw ("clipboard image write failed after retries: " + $lastError)
}

function Paste-Image([string]$Path) {
  [void](Set-ClipboardImageSafe $Path)
  Start-Sleep -Milliseconds 180
  [NmfStable]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x56, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x56, 0, 2, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 600
}

function Read-ImageText([string]$Path) {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
  $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
  $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]

  function Await-WinRtOperation($Operation, [type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq "AsTask" -and
      $_.IsGenericMethod -and
      $_.GetParameters().Count -eq 1 -and
      $_.GetGenericArguments().Count -eq 1 -and
      $_.ToString().StartsWith('System.Threading.Tasks.Task`1')
    } | Select-Object -First 1
    $generic = $method.MakeGenericMethod($ResultType)
    $task = $generic.Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
  }

  $file = Await-WinRtOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await-WinRtOperation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-WinRtOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-WinRtOperation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) {
    throw "Windows OCR engine is not available"
  }
  $result = Await-WinRtOperation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $result.Text
}

function Read-ImageOcrLines([string]$Path) {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
  $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
  $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]

  function Await-WinRtOperation($Operation, [type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq "AsTask" -and
      $_.IsGenericMethod -and
      $_.GetParameters().Count -eq 1 -and
      $_.GetGenericArguments().Count -eq 1 -and
      $_.ToString().StartsWith('System.Threading.Tasks.Task`1')
    } | Select-Object -First 1
    $generic = $method.MakeGenericMethod($ResultType)
    $task = $generic.Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
  }

  $file = Await-WinRtOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await-WinRtOperation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-WinRtOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-WinRtOperation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) {
    throw "Windows OCR engine is not available"
  }
  $result = Await-WinRtOperation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $rows = New-Object System.Collections.ArrayList
  foreach ($line in @($result.Lines)) {
    $words = @($line.Words)
    if (-not $words -or $words.Count -eq 0) { continue }
    $left = [double]::PositiveInfinity
    $top = [double]::PositiveInfinity
    $right = 0.0
    $bottom = 0.0
    foreach ($word in $words) {
      $rect = $word.BoundingRect
      $left = [Math]::Min($left, [double]$rect.X)
      $top = [Math]::Min($top, [double]$rect.Y)
      $right = [Math]::Max($right, [double]($rect.X + $rect.Width))
      $bottom = [Math]::Max($bottom, [double]($rect.Y + $rect.Height))
    }
    [void]$rows.Add([pscustomobject]@{
      Text = $line.Text
      Left = $left
      Top = $top
      Width = $right - $left
      Height = $bottom - $top
    })
  }
  @($rows)
}

function Get-MemberSearchProbe($Frame, [string]$ShotPath) {
  $result = [ordered]@{
    Y = 0
    Text = ""
    Strong = $false
    Reason = "none"
  }
  try {
    $rightPanelMinX = [Math]::Max(0, $Frame.Width - 430)
    $rightPanelMaxX = $Frame.Width + 8
    $topMin = 85
    $topMax = [Math]::Min([Math]::Max(360, [int]($Frame.Height * 0.55)), $Frame.Height - 90)
    $weakCandidate = $null
    foreach ($line in @(Read-ImageOcrLines $ShotPath)) {
      $text = (($line.Text + "") -replace "\s+", "")
      $centerX = [double]$line.Left + ([double]$line.Width / 2.0)
      $centerY = [double]$line.Top + ([double]$line.Height / 2.0)
      if ($centerX -lt $rightPanelMinX -or $centerX -gt $rightPanelMaxX) { continue }
      if ($centerY -lt $topMin -or $centerY -gt $topMax) { continue }
      if ($text -like "*群成员*") {
        $y = [int]($Frame.Top + [double]$line.Top + ([double]$line.Height / 2.0))
        $result.Y = $y
        $result.Text = $text
        $result.Strong = $true
        $result.Reason = "ocr-group-member"
        Write-TraceStage ("member-search-probe strong text=" + $text + " y=" + $y)
        return [pscustomobject]$result
      }
      if ($text -like "*成员*" -and -not $weakCandidate) {
        $weakCandidate = [pscustomobject]@{
          Y = [int]($Frame.Top + [double]$line.Top + ([double]$line.Height / 2.0))
          Text = $text
        }
      }
    }
    if ($weakCandidate) {
      $result.Y = [int]$weakCandidate.Y
      $result.Text = $weakCandidate.Text
      $result.Strong = $false
      $result.Reason = "ocr-member-weak"
      Write-TraceStage ("member-search-probe weak text=" + $result.Text + " y=" + $result.Y)
      return [pscustomobject]$result
    }
  } catch {
    $result.Reason = "ocr-failed"
    Write-TraceStage ("member-search-probe-ocr-failed " + $_.Exception.Message)
  }
  [pscustomobject]$result
}

function Get-MemberSearchY($Frame, [string]$ShotPath) {
  $probe = Get-MemberSearchProbe $Frame $ShotPath
  return [int]$probe.Y
}

function Assert-ProfileQQ([string]$ShotPath, [string]$ExpectedQQ) {
  if (-not $ExpectedQQ) {
    return ""
  }
  $ocrText = Read-ImageText $ShotPath
  $digits = ($ocrText -replace "[^0-9]", " ")
  $parts = @($digits -split '\s+' | Where-Object { $_ })
  $tokens = @($ocrText -split '[^0-9A-Za-z]+' | Where-Object { $_ })
  $normalizedTokens = @()
  foreach ($token in $tokens) {
    $normalized = $token.ToUpperInvariant()
    $normalized = $normalized.Replace("O", "0").Replace("Q", "0")
    $normalized = $normalized.Replace("I", "1").Replace("L", "1")
    $normalized = $normalized.Replace("S", "5")
    $normalized = $normalized.Replace("B", "8")
    $normalized = $normalized.Replace("Z", "2")
    if ($normalized -match '^[0-9]+$') {
      $normalizedTokens += $normalized
    }
  }
  if (($parts -notcontains $ExpectedQQ) -and ($normalizedTokens -notcontains $ExpectedQQ)) {
    throw ("profile QQ mismatch; expected=" + $ExpectedQQ + "; ocr=" + $ocrText)
  }
  $ocrText
}

function Press-Enter {
  [NmfStable]::keybd_event(0x0D, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x0D, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 700
}

function Press-Escape {
  [NmfStable]::keybd_event(0x1B, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x1B, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 350
}

function Dismiss-BlockingDialog($Frame) {
  Write-TraceStage "dismiss-blocking-dialog"
  try {
    Press-Enter
    Start-Sleep -Milliseconds 250
  } catch {}
  try {
    Press-Escape
    Start-Sleep -Milliseconds 250
  } catch {}
  try {
    $okX = [int]($Frame.Left + ($Frame.Width * 0.57))
    $okY = [int]($Frame.Top + ($Frame.Height * 0.56))
    Write-TraceStage ("dismiss-blocking-dialog-click x=" + $okX + " y=" + $okY)
    Click-At $okX $okY
  } catch {}
  try {
    Press-Escape
  } catch {}
}

function Save-Shot($Frame, [string]$Name) {
  $path = Join-Path $OutDir $Name
  $bmp = New-Object System.Drawing.Bitmap $Frame.Width, $Frame.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($Frame.Left, $Frame.Top, 0, 0, [System.Drawing.Size]::new($Frame.Width, $Frame.Height))
  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose()
  $bmp.Dispose()
  $path
}

function Get-ElementHints($Element) {
  $hints = New-Object System.Collections.ArrayList
  foreach ($prop in @("Name", "AutomationId", "ClassName", "HelpText")) {
    try {
      $value = $Element.Current.$prop
      if (($value + "").Trim()) {
        [void]$hints.Add(($value + "").Trim())
      }
    } catch {}
  }
  @($hints)
}

function Get-GroupPanelAutomationScore($Frame) {
  $result = [ordered]@{
    Available = $script:UiaAvailable
    Score = 0
    Hints = @()
  }
  if (-not $script:UiaAvailable) {
    return [pscustomobject]$result
  }

  $memberText = -join ([char[]](0x7fa4, 0x804a, 0x6210, 0x5458))
  $noticeText = -join ([char[]](0x7fa4, 0x516c, 0x544a))
  $searchText = -join ([char[]](0x641c, 0x7d22))
  $ownerText = -join ([char[]](0x7fa4, 0x4e3b))
  $adminText = -join ([char[]](0x7ba1, 0x7406, 0x5458))

  try {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($Frame.Handle)
    if (-not $root) {
      return [pscustomobject]$result
    }
    $all = $root.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
    $rightMin = $Frame.Left + [Math]::Max(0, $Frame.Width - 390)
    $rightMax = $Frame.Left + $Frame.Width + 8
    $topMin = $Frame.Top + 115
    $hits = New-Object System.Collections.ArrayList
    $score = 0
    $limit = [Math]::Min($all.Count, 800)
    for ($i = 0; $i -lt $limit; $i++) {
      $el = $all.Item($i)
      try {
        $rect = $el.Current.BoundingRectangle
        if ($rect.Width -le 0 -or $rect.Height -le 0) { continue }
        $cx = $rect.X + ($rect.Width / 2.0)
        $cy = $rect.Y + ($rect.Height / 2.0)
        if ($cx -lt $rightMin -or $cx -gt $rightMax -or $cy -lt $topMin) { continue }
      } catch {
        continue
      }

      foreach ($hint in @(Get-ElementHints $el)) {
        if ($hint -like "*$memberText*") {
          $score += 50
          if (-not $hits.Contains($hint)) { [void]$hits.Add($hint) }
        } elseif ($hint -like "*$noticeText*") {
          $score += 20
          if (-not $hits.Contains($hint)) { [void]$hits.Add($hint) }
        } elseif ($hint -like "*$ownerText*" -or $hint -like "*$adminText*") {
          $score += 10
          if (-not $hits.Contains($hint)) { [void]$hits.Add($hint) }
        } elseif ($hint -like "*$searchText*") {
          $score += 6
          if ($hits.Count -lt 8 -and -not $hits.Contains($hint)) { [void]$hits.Add($hint) }
        }
      }
    }
    $result.Score = $score
    $result.Hints = @($hits)
  } catch {
    $result.Available = $false
  }
  [pscustomobject]$result
}

function Get-GroupPanelPixelScore($Frame) {
  $lineHeight = [Math]::Min(520, [Math]::Max(160, $Frame.Height - 250))
  $x = $Frame.Left + $Frame.Width - 275
  $y = $Frame.Top + 135
  $bmp = New-Object System.Drawing.Bitmap 1, $lineHeight
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($x, $y, 0, 0, [System.Drawing.Size]::new(1, $lineHeight))
  $g.Dispose()
  $score = 0
  for ($i = 0; $i -lt $lineHeight; $i++) {
    $c = $bmp.GetPixel(0, $i)
    $b = ($c.R + $c.G + $c.B) / 3.0
    if ($b -ge 42 -and $b -le 70) {
      $score += 1
    }
  }
  $bmp.Dispose()
  [pscustomobject]@{
    Score = $score
    Height = $lineHeight
    Ratio = $score / [double]$lineHeight
  }
}

function Get-GroupPanelScore($Frame) {
  $pixel = Get-GroupPanelPixelScore $Frame
  $automation = Get-GroupPanelAutomationScore $Frame
  $detected = ($pixel.Ratio -gt 0.55) -or ($automation.Score -ge 20)
  [pscustomobject]@{
    Score = $pixel.Score
    Height = $pixel.Height
    Ratio = $pixel.Ratio
    AutomationAvailable = $automation.Available
    AutomationScore = $automation.Score
    AutomationHints = @($automation.Hints)
    GroupPanelDetected = $detected
  }
}

function Get-ImageDifferenceRatio([string]$PathA, [string]$PathB) {
  if (-not $PathA -or -not $PathB) { return 1.0 }
  if (-not (Test-Path -LiteralPath $PathA) -or -not (Test-Path -LiteralPath $PathB)) { return 1.0 }
  $bmpA = $null
  $bmpB = $null
  try {
    $bmpA = [System.Drawing.Bitmap]::FromFile($PathA)
    $bmpB = [System.Drawing.Bitmap]::FromFile($PathB)
    $width = [Math]::Min($bmpA.Width, $bmpB.Width)
    $height = [Math]::Min($bmpA.Height, $bmpB.Height)
    if ($width -le 0 -or $height -le 0) { return 1.0 }
    $stepX = [Math]::Max(12, [int]($width / 90))
    $stepY = [Math]::Max(12, [int]($height / 70))
    $total = 0
    $different = 0
    for ($y = 0; $y -lt $height; $y += $stepY) {
      for ($x = 0; $x -lt $width; $x += $stepX) {
        $a = $bmpA.GetPixel($x, $y)
        $b = $bmpB.GetPixel($x, $y)
        $delta = [Math]::Abs($a.R - $b.R) + [Math]::Abs($a.G - $b.G) + [Math]::Abs($a.B - $b.B)
        if ($delta -gt 45) { $different += 1 }
        $total += 1
      }
    }
    if ($total -le 0) { return 1.0 }
    return ($different / [double]$total)
  } catch {
    Write-TraceStage ("image-diff-failed " + $_.Exception.Message)
    return 1.0
  } finally {
    if ($bmpA) { $bmpA.Dispose() }
    if ($bmpB) { $bmpB.Dispose() }
  }
}

function Wait-ForGroupPanel($Frame, [int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  $last = $null
  while ((Get-Date) -lt $deadline) {
    $last = Get-GroupPanelScore $Frame
    if ($last.GroupPanelDetected) {
      return $last
    }
    Start-Sleep -Milliseconds 500
  }
  if ($TargetQQ) {
    return $last
  }
  throw ("group member panel was not detected; score=" + ($last | ConvertTo-Json -Compress))
}

function Get-GroupPageProbe($Frame, [string]$ShotPath) {
  $score = Get-GroupPanelScore $Frame
  $memberProbe = Get-MemberSearchProbe $Frame $ShotPath
  $memberSearchY = [int]$memberProbe.Y
  $memberStrong = [bool]$memberProbe.Strong
  $pixelAssist = [bool]($score.Ratio -gt 0.035)
  $automationAssist = [bool]($score.AutomationScore -gt 0)
  $looksLikeGroup = [bool](
    $score.GroupPanelDetected -or
    $memberStrong -or
    ($memberSearchY -gt 0 -and ($pixelAssist -or $automationAssist))
  )
  $confidence = "none"
  if ($score.GroupPanelDetected) {
    $confidence = "group-panel"
  } elseif ($memberStrong) {
    $confidence = "ocr-group-member"
  } elseif ($memberSearchY -gt 0 -and ($pixelAssist -or $automationAssist)) {
    $confidence = "weak-ocr-assisted"
  } elseif ($memberSearchY -gt 0) {
    $confidence = "weak-ocr-only"
  }
  [pscustomobject]@{
    LooksLikeGroup = $looksLikeGroup
    Confidence = $confidence
    Score = $score
    MemberSearchY = $memberSearchY
    MemberSearchText = $memberProbe.Text
    MemberSearchStrong = $memberStrong
    MemberSearchReason = $memberProbe.Reason
    Shot = $ShotPath
  }
}

function Test-CurrentPageLooksLikeGroup($Frame) {
  $probeShot = Save-Shot $Frame "02-current-page-before-decision.png"
  [void]$shots.Add($probeShot)
  $probe = Get-GroupPageProbe $Frame $probeShot
  Write-TraceStage ("current-page-group-probe looksLikeGroup=" + $probe.LooksLikeGroup + " confidence=" + $probe.Confidence + " memberSearchY=" + $probe.MemberSearchY + " memberText=" + $probe.MemberSearchText + " score=" + ($probe.Score | ConvertTo-Json -Compress))
  $probe
}

function Wait-ForStableGroupPage($Frame, [int]$Seconds, [string]$Prefix, [int]$RequiredHits = 2) {
  $deadline = (Get-Date).AddSeconds([Math]::Max(1, $Seconds))
  $hits = 0
  $attempt = 0
  $last = $null
  while ((Get-Date) -lt $deadline) {
    $attempt += 1
    $fresh = Get-MainQQWindow
    $script:MainHandleValue = $fresh.HandleValue
    $shot = Save-Shot $fresh ($Prefix + "-group-stability-" + $attempt + ".png")
    [void]$shots.Add($shot)
    $probe = Get-GroupPageProbe $fresh $shot
    $last = $probe
    if ($probe.LooksLikeGroup) {
      $hits += 1
    } else {
      $hits = 0
    }
    Write-TraceStage ("stable-group-probe prefix=" + $Prefix + " attempt=" + $attempt + " hits=" + $hits + " looksLikeGroup=" + $probe.LooksLikeGroup + " confidence=" + $probe.Confidence + " memberSearchY=" + $probe.MemberSearchY + " score=" + ($probe.Score | ConvertTo-Json -Compress))
    if ($hits -ge $RequiredHits) {
      $probe | Add-Member -NotePropertyName Stable -NotePropertyValue $true -Force
      return $probe
    }
    Start-Sleep -Milliseconds 450
  }
  if (-not $last) {
    $last = [pscustomobject]@{
      LooksLikeGroup = $false
      Confidence = "none"
      Score = $null
      MemberSearchY = 0
      MemberSearchText = ""
      MemberSearchStrong = $false
      MemberSearchReason = "timeout"
      Shot = ""
    }
  }
  $last | Add-Member -NotePropertyName Stable -NotePropertyValue $false -Force
  $last
}

function Assert-PrivateChat($Frame, [string]$ShotPath, [string]$ExpectedQQ) {
  $score = Get-GroupPanelScore $Frame
  $ratioLimit = 0.12
  if ($script:LastGroupLikeShot) {
    $ratioLimit = 0.035
  }
  if ($score.GroupPanelDetected -or $score.Ratio -gt $ratioLimit) {
    throw ("private chat guard refused to send; group panel still visible; score=" + ($score | ConvertTo-Json -Compress))
  }
  if ($ShotPath) {
    try {
      $ocrText = ((Read-ImageText $ShotPath) + "") -replace "\s+", ""
      $groupHints = @("群成员", "群公告", "群主", "管理员")
      foreach ($hint in $groupHints) {
        if ($ocrText -like "*$hint*") {
          throw ("private chat guard refused to send; group page OCR hint=" + $hint)
        }
      }
      if ($ExpectedQQ -and $ocrText -like ("*" + $ExpectedQQ + "*")) {
        throw ("private chat guard refused to send; target QQ still visible on possible group search page")
      }
    } catch {
      if (($_.Exception.Message + "") -like "private chat guard refused*") { throw }
      Write-TraceStage ("private-chat-ocr-guard-soft-failed " + $_.Exception.Message)
    }
  }
  if ($script:LastGroupLikeShot -and $ShotPath) {
    $diffRatio = Get-ImageDifferenceRatio $script:LastGroupLikeShot $ShotPath
    Write-TraceStage ("private-chat-vs-group-diff-ratio=" + $diffRatio)
    if ($diffRatio -lt 0.08) {
      throw ("private chat guard refused to send; current page still looks like group search page; diffRatio=" + $diffRatio)
    }
  }
  $score
}

function Wait-ForPrivateChatQuick([int]$Milliseconds, [string]$Prefix) {
  $deadline = (Get-Date).AddMilliseconds([Math]::Max(300, $Milliseconds))
  $attempt = 0
  while ((Get-Date) -lt $deadline) {
    $attempt += 1
    try {
      $main = Get-MainQQWindow
      $script:MainHandleValue = $main.HandleValue
      Focus-Maximized $main.Handle
      $main = Get-MainQQWindow
      $script:MainHandleValue = $main.HandleValue
      $shot = Save-Shot $main ($Prefix + "-private-chat-probe-" + $attempt + ".png")
      [void]$shots.Add($shot)
      [void](Assert-PrivateChat $main $shot $TargetQQ)
      Write-TraceStage ("private-chat-opened-by-context-menu attempt=" + $attempt)
      return $main
    } catch {
      Write-TraceStage ("private-chat-probe-not-ready " + $Prefix + " attempt=" + $attempt + " reason=" + $_.Exception.Message)
    }
    Start-Sleep -Milliseconds 280
  }
  return $null
}

function Click-ContextMenuSendMessage([object]$Frame, [int]$RightClickX, [int]$RightClickY, [string]$Prefix) {
  $menuShot = Save-Shot $Frame ($Prefix + "-context-menu.png")
  [void]$shots.Add($menuShot)
  try {
    foreach ($line in @(Read-ImageOcrLines $menuShot)) {
      $text = (($line.Text + "") -replace "\s+", "")
      if (-not ($text -like "*发送消息*" -or $text -like "*发消息*")) { continue }
      $centerX = [int]($Frame.Left + [double]$line.Left + ([double]$line.Width / 2.0))
      $centerY = [int]($Frame.Top + [double]$line.Top + ([double]$line.Height / 2.0))
      if ([Math]::Abs($centerX - $RightClickX) -gt 280) { continue }
      if ($centerY -lt ($RightClickY - 220) -or $centerY -gt ($RightClickY + 280)) { continue }
      Write-TraceStage ("click-context-menu-send-message-ocr x=" + $centerX + " y=" + $centerY + " text=" + $text)
      Click-At $centerX $centerY
      return $true
    }
  } catch {
    Write-TraceStage ("context-menu-send-message-ocr-failed " + $_.Exception.Message)
  }

  $fallbackX = [int]([Math]::Max($Frame.Left + 20, [Math]::Min($Frame.Left + $Frame.Width - 20, $RightClickX + 72)))
  $fallbackY = [int]([Math]::Max($Frame.Top + 20, [Math]::Min($Frame.Top + $Frame.Height - 20, $RightClickY + 18)))
  Write-TraceStage ("click-context-menu-send-message-fallback x=" + $fallbackX + " y=" + $fallbackY)
  Click-At $fallbackX $fallbackY
  return $true
}

function Close-ProfilePopups {
  foreach ($popup in @(Find-QQWindows | Where-Object {
    $title = $_.Title + ""
    $_.HandleValue -ne $script:MainHandleValue -and
    ($title -like "*$script:NoticeTitle*" -or $_.Title -eq $script:ProfileTitle -or ($_.Width -ge 300 -and $_.Width -le 900 -and $_.Height -ge 300 -and $_.Height -le 1000))
  })) {
    [NmfStable]::PostMessage($popup.Handle, 0x0010, [UIntPtr]::Zero, [UIntPtr]::Zero) | Out-Null
  }
  Start-Sleep -Milliseconds 400
}

function Get-ProfileCandidate {
  $windows = @(Find-QQWindows | Where-Object {
    $title = $_.Title + ""
    $mainArea = 0
    if ($script:MainHandleValue) {
      try {
        $mainWindow = Find-QQWindows | Where-Object { $_.HandleValue -eq $script:MainHandleValue } | Select-Object -First 1
        if ($mainWindow) { $mainArea = [int64]$mainWindow.Area }
      } catch {}
    }
    $_.Visible -and
    $_.HandleValue -ne $script:MainHandleValue -and
    $title -notlike "*$script:NoticeTitle*" -and
    $_.Left -gt -30000 -and
    $_.Top -gt -30000 -and
    $_.Width -gt 220 -and
    $_.Height -gt 220 -and
    $_.Width -lt 1800 -and
    $_.Height -lt 1800 -and
    ($mainArea -le 0 -or [int64]$_.Area -lt [int64]($mainArea * 0.75))
  })
  $exact = $windows | Where-Object { $_.Title -eq $script:ProfileTitle } | Select-Object -First 1
  if ($exact) { return $exact }
  $fallback = $windows | Sort-Object Area -Descending | Select-Object -First 1
  if ($fallback) { return $fallback }
  return $null
}

function Wait-ForProfile([int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    $profile = Get-ProfileCandidate
    if ($profile) {
      return $profile
    }
    Start-Sleep -Milliseconds 500
  }
  try {
    $candidates = @(Find-QQWindows | Where-Object { $_.Visible -and $_.HandleValue -ne $script:MainHandleValue } | Select-Object -First 8 Title,Class,Left,Top,Width,Height,Area,HandleValue)
    Write-TraceStage ("profile-not-detected windows=" + ($candidates | ConvertTo-Json -Compress))
  } catch {}
  throw "profile card was not detected"
}

function Try-WaitForProfile([int]$Seconds) {
  try {
    return Wait-ForProfile $Seconds
  } catch {
    return $null
  }
}

function Wait-ForProfileQuick([int]$Milliseconds) {
  $deadline = (Get-Date).AddMilliseconds([Math]::Max(100, $Milliseconds))
  while ((Get-Date) -lt $deadline) {
    $profile = Get-ProfileCandidate
    if ($profile) {
      return $profile
    }
    Start-Sleep -Milliseconds 120
  }
  return $null
}

function New-SearchResultClickPoints($MainFrame, [int]$BaseY) {
  $right = [int]($MainFrame.Left + $MainFrame.Width)
  $top = [int]$MainFrame.Top
  $rowY = [int]($top + $BaseY)
  $points = New-Object System.Collections.ArrayList
  [void]$points.Add([pscustomobject]@{ X = $right - 238; Y = $rowY; Label = "first-avatar" })
  [void]$points.Add([pscustomobject]@{ X = $right - 210; Y = $rowY; Label = "first-name" })
  [void]$points.Add([pscustomobject]@{ X = $right - 260; Y = $rowY; Label = "first-left" })
  [void]$points.Add([pscustomobject]@{ X = $right - 238; Y = $rowY - 18; Label = "first-avatar-high" })
  [void]$points.Add([pscustomobject]@{ X = $right - 210; Y = $rowY - 18; Label = "first-name-high" })
  [void]$points.Add([pscustomobject]@{ X = $right - 238; Y = $rowY + 18; Label = "first-avatar-low" })
  return $points
}

function New-PrivateChatOpenedResult($Frame, [string]$Via) {
  [pscustomobject]@{
    PrivateChatOpened = $true
    Via = $Via
    HandleValue = $Frame.HandleValue
    Left = $Frame.Left
    Top = $Frame.Top
    Width = $Frame.Width
    Height = $Frame.Height
  }
}

function Open-SearchResultPrivateChat($MainFrame, [int]$BaseY, $Calibration = $null, [string]$Prefix = "search") {
  $points = New-SearchResultClickPoints $MainFrame $BaseY
  $orderedPoints = New-Object System.Collections.ArrayList
  $calibratedResult = Get-CalibratedPoint $Calibration "searchResultFirst" $MainFrame
  if ($calibratedResult) {
    $script:CalibrationUsed = $true
    [void]$orderedPoints.Add([pscustomobject]@{ X = $calibratedResult.X; Y = $calibratedResult.Y; Label = "calibrated-first" })
  }
  foreach ($pt in $points) {
    [void]$orderedPoints.Add($pt)
  }

  foreach ($pt in $orderedPoints) {
    Write-TraceStage ("right-click-search-result-send-message " + $Prefix + " " + $pt.Label + " x=" + $pt.X + " y=" + $pt.Y)
    RightClick-At ([int]$pt.X) ([int]$pt.Y)
    $fresh = Get-MainQQWindow
    $script:MainHandleValue = $fresh.HandleValue
    [void](Click-ContextMenuSendMessage $fresh ([int]$pt.X) ([int]$pt.Y) ($Prefix + "-" + $pt.Label))
    $privateFrame = Wait-ForPrivateChatQuick 3600 ($Prefix + "-" + $pt.Label)
    if ($privateFrame) {
      return (New-PrivateChatOpenedResult $privateFrame ("context-menu-" + $pt.Label))
    }
    Press-Escape
    Start-Sleep -Milliseconds 250
  }
  return $null
}

function Open-SearchResultProfile($MainFrame, [int]$BaseY) {
  $right = [int]($MainFrame.Left + $MainFrame.Width)
  $top = [int]$MainFrame.Top
  $rowY = [int]($top + $BaseY)
  $points = New-SearchResultClickPoints $MainFrame $BaseY

  foreach ($pt in $points) {
    Write-TraceStage ("click-search-result " + $pt.Label + " x=" + $pt.X + " y=" + $pt.Y)
    Click-At ([int]$pt.X) ([int]$pt.Y)
    $profile = Wait-ForProfileQuick 1650
    if ($profile) { return $profile }
  }

  foreach ($idx in @(0, 1, 3)) {
    $pt = $points[$idx]
    Write-TraceStage ("double-click-search-result " + $pt.Label + " x=" + $pt.X + " y=" + $pt.Y)
    DoubleClick-At ([int]$pt.X) ([int]$pt.Y)
    $profile = Wait-ForProfileQuick 1900
    if ($profile) { return $profile }
  }

  foreach ($dy in @(-34, 0, 34)) {
    foreach ($dx in @(-250, -205)) {
      $x = [int]($right + $dx)
      $y = [int]($rowY + $dy)
      Write-TraceStage ("last-click-search-result x=" + $x + " y=" + $y)
      Click-At $x $y
      $profile = Wait-ForProfileQuick 1450
      if ($profile) { return $profile }
    }
  }

  return $null
}

function New-RelativePoint($Frame, [int]$X, [int]$Y, [string]$Anchor) {
  [ordered]@{
    anchor = $Anchor
    x = $X
    y = $Y
    relativeX = [Math]::Round((($X - [double]$Frame.Left) / [double]$Frame.Width), 6)
    relativeY = [Math]::Round((($Y - [double]$Frame.Top) / [double]$Frame.Height), 6)
    frame = @{
      left = $Frame.Left
      top = $Frame.Top
      width = $Frame.Width
      height = $Frame.Height
    }
  }
}

function Read-Calibration([string]$Path) {
  if (-not $Path) { return $null }
  try {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $data = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($data -and $data.points) {
      Write-TraceStage ("calibration-loaded " + $Path)
      return $data
    }
  } catch {
    Write-TraceStage ("calibration-load-failed " + $_.Exception.Message)
  }
  return $null
}

function Test-CalibrationMainFrame($Calibration, $Frame) {
  if (-not $Calibration -or -not $Calibration.main -or -not $Frame) { return $true }
  $main = $Calibration.main
  if ($null -eq $main.width -or $null -eq $main.height) { return $true }
  $widthDelta = [Math]::Abs([int]$main.width - [int]$Frame.Width)
  $heightDelta = [Math]::Abs([int]$main.height - [int]$Frame.Height)
  $leftDelta = 0
  $topDelta = 0
  if ($null -ne $main.left) { $leftDelta = [Math]::Abs([int]$main.left - [int]$Frame.Left) }
  if ($null -ne $main.top) { $topDelta = [Math]::Abs([int]$main.top - [int]$Frame.Top) }
  $ok = ($widthDelta -le 180 -and $heightDelta -le 180 -and $leftDelta -le 180 -and $topDelta -le 180)
  if (-not $ok) {
    Write-TraceStage ("calibration-main-frame-mismatch widthDelta=" + $widthDelta + " heightDelta=" + $heightDelta + " leftDelta=" + $leftDelta + " topDelta=" + $topDelta)
  }
  return $ok
}

function Get-CalibratedPoint($Calibration, [string]$Name, $Frame) {
  if (-not $Calibration -or -not $Calibration.points) { return $null }
  $prop = $Calibration.points.PSObject.Properties[$Name]
  if (-not $prop) { return $null }
  $pt = $prop.Value
  if ($null -eq $pt.relativeX -or $null -eq $pt.relativeY) { return $null }
  if (($pt.anchor + "") -eq "main" -and -not (Test-CalibrationMainFrame $Calibration $Frame)) { return $null }
  [pscustomobject]@{
    X = [int]([Math]::Round($Frame.Left + ([double]$Frame.Width * [double]$pt.relativeX)))
    Y = [int]([Math]::Round($Frame.Top + ([double]$Frame.Height * [double]$pt.relativeY)))
    Name = $Name
    Anchor = $pt.anchor
  }
}

function Get-StoredCalibrationPoint($Calibration, [string]$Name) {
  if (-not $Calibration -or -not $Calibration.points) { return $null }
  $prop = $Calibration.points.PSObject.Properties[$Name]
  if (-not $prop) { return $null }
  $pt = $prop.Value
  if ($null -eq $pt.x -or $null -eq $pt.y) { return $null }
  [pscustomobject]@{
    X = [int]$pt.x
    Y = [int]$pt.y
    Name = $Name
    Anchor = $pt.anchor
    Frame = $pt.frame
  }
}

function Wait-CalibratedCursorPoint($Frame, [string]$Name, [string]$Anchor, [string]$Title, [string]$Instruction, [int]$TimeoutSeconds) {
  Write-TraceStage ("calibrate-wait " + $Name)
  $form = New-Object System.Windows.Forms.Form
  $form.Text = "新人欢迎校准"
  $form.TopMost = $true
  $form.Width = 520
  $form.Height = 180
  $form.StartPosition = "Manual"
  $form.Left = [int]($Frame.Left + 20)
  $form.Top = [int]($Frame.Top + 30)
  $label = New-Object System.Windows.Forms.Label
  $label.Dock = "Fill"
  $label.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11)
  $label.Padding = New-Object System.Windows.Forms.Padding(14)
  $label.Text = ($Title + "`r`n`r`n" + $Instruction + "`r`n`r`n把鼠标移动到目标位置，按 F8 记录，按 ESC 取消。这个提示窗可以拖动。")
  $form.Controls.Add($label)
  [void]$form.Show()
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  try {
    while ((Get-Date) -lt $deadline) {
      [System.Windows.Forms.Application]::DoEvents()
      if (([NmfStable]::GetAsyncKeyState(0x1B) -band 0x8000) -ne 0) {
        throw "calibration canceled by ESC"
      }
      if (([NmfStable]::GetAsyncKeyState(0x77) -band 0x8000) -ne 0) {
        while (([NmfStable]::GetAsyncKeyState(0x77) -band 0x8000) -ne 0) {
          Start-Sleep -Milliseconds 40
          [System.Windows.Forms.Application]::DoEvents()
        }
        $cursor = New-Object NmfStable+POINT
        [NmfStable]::GetCursorPos([ref]$cursor) | Out-Null
        Write-TraceStage ("calibrate-captured " + $Name + " x=" + $cursor.X + " y=" + $cursor.Y)
        return New-RelativePoint $Frame ([int]$cursor.X) ([int]$cursor.Y) $Anchor
      }
      Start-Sleep -Milliseconds 60
    }
  } finally {
    try { $form.Close(); $form.Dispose() } catch {}
  }
  throw ("calibration timeout waiting for " + $Name)
}

function Invoke-Calibration {
  if (-not $CalibrationFile) {
    throw "CalibrationFile is required in calibrate mode"
  }
  Write-TraceStage "calibrate-start"
  Close-ProfilePopups
  $mainFrame = Get-MainQQWindow
  $script:MainHandleValue = $mainFrame.HandleValue
  Focus-Maximized $mainFrame.Handle
  $mainFrame = Get-MainQQWindow
  $script:MainHandleValue = $mainFrame.HandleValue
  [void]$shots.Add((Save-Shot $mainFrame "calibrate-01-main-start.png"))

  $points = [ordered]@{}
  $points.pinnedGroup = Wait-CalibratedCursorPoint $mainFrame "pinnedGroup" "main" "1/5 置顶目标群" "把鼠标放到左侧会话列表里的置顶目标群这一行，不要点击，然后按 F8。" 180
  $points.memberSearchInput = Wait-CalibratedCursorPoint $mainFrame "memberSearchInput" "main" "2/5 群成员搜索框" "把鼠标放到右侧群成员栏的搜索框或搜索图标上，不要点击，然后按 F8。" 180

  $memberSearch = Get-CalibratedPoint ([pscustomobject]@{ points = [pscustomobject]$points }) "memberSearchInput" $mainFrame
  Click-At $memberSearch.X $memberSearch.Y
  Press-CtrlA-Backspace
  if ($TargetQQ) {
    Write-TraceStage "calibrate-type-target-qq"
    if (-not (Type-Digits $TargetQQ)) {
      Paste-Text $TargetQQ
    }
    Start-Sleep -Seconds 1
  }
  [void]$shots.Add((Save-Shot $mainFrame "calibrate-02-after-search-input.png"))

  $points.searchResultFirst = Wait-CalibratedCursorPoint $mainFrame "searchResultFirst" "main" "3/5 第一条搜索结果" "把鼠标放到右侧第一条搜索结果的头像或昵称上，不要点击，然后按 F8。" 180
  $resultPoint = Get-CalibratedPoint ([pscustomobject]@{ points = [pscustomobject]$points }) "searchResultFirst" $mainFrame
  Click-At $resultPoint.X $resultPoint.Y
  $profileFrame = Try-WaitForProfile 8
  if ($profileFrame) {
    [void]$shots.Add((Save-Shot $profileFrame "calibrate-03-profile.png"))
    $points.profileSendButton = Wait-CalibratedCursorPoint $profileFrame "profileSendButton" "profile" "4/5 资料卡发消息按钮" "把鼠标放到资料卡里的发消息按钮上，不要点击，然后按 F8。" 180
    $sendPoint = Get-CalibratedPoint ([pscustomobject]@{ points = [pscustomobject]$points }) "profileSendButton" $profileFrame
    Click-At $sendPoint.X $sendPoint.Y
  } else {
    [System.Windows.Forms.MessageBox]::Show("没有自动打开资料卡。请手动打开任意群成员资料卡，然后点这里的确定继续校准。", "新人欢迎校准", "OK", "Information") | Out-Null
    $profileFrame = Try-WaitForProfile 30
    if (-not $profileFrame) {
      throw "profile card was not detected during calibration"
    }
    [void]$shots.Add((Save-Shot $profileFrame "calibrate-03-profile-manual.png"))
    $points.profileSendButton = Wait-CalibratedCursorPoint $profileFrame "profileSendButton" "profile" "4/5 资料卡发消息按钮" "把鼠标放到资料卡里的发消息按钮上，不要点击，然后按 F8。" 180
    $sendPoint = Get-CalibratedPoint ([pscustomobject]@{ points = [pscustomobject]$points }) "profileSendButton" $profileFrame
    Click-At $sendPoint.X $sendPoint.Y
  }
  Start-Sleep -Seconds 1
  $mainFrame = Get-MainQQWindow
  $script:MainHandleValue = $mainFrame.HandleValue
  Focus-Maximized $mainFrame.Handle
  [void]$shots.Add((Save-Shot $mainFrame "calibrate-04-private-chat.png"))
  $points.privateChatInput = Wait-CalibratedCursorPoint $mainFrame "privateChatInput" "main" "5/5 私聊输入框" "把鼠标放到底部私聊输入框里，不要点击，然后按 F8。" 180

  $payload = [ordered]@{
    version = 1
    updatedAt = (Get-Date).ToString("o")
    targetQQ = $TargetQQ
    main = @{
      left = $mainFrame.Left
      top = $mainFrame.Top
      width = $mainFrame.Width
      height = $mainFrame.Height
    }
    points = $points
  }
  $parent = Split-Path -Parent $CalibrationFile
  if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  ($payload | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $CalibrationFile -Encoding UTF8
  Write-TraceStage ("calibrate-saved " + $CalibrationFile)
  $result = [ordered]@{
    mode = "calibrate"
    ok = $true
    sent = $false
    calibrationFile = $CalibrationFile
    points = $points
    shots = @($shots)
    outDir = $OutDir
  }
  ($result | ConvertTo-Json -Depth 8) | Set-Content -Path (Join-Path $OutDir "result.json") -Encoding UTF8
  $result | ConvertTo-Json -Depth 8
}

function Invoke-MemberSearchFromPage($MainFrame, [string]$Prefix, $Calibration = $null) {
  $mainLeft = [int]$MainFrame.Left
  $mainTop = [int]$MainFrame.Top
  $mainWidth = [int]$MainFrame.Width
  $searchRight = $mainLeft + $mainWidth
  $panelShot = Save-Shot $MainFrame ($Prefix + "-member-panel-before-search.png")
  [void]$shots.Add($panelShot)
  $panelProbe = Get-GroupPageProbe $MainFrame $panelShot
  $calibratedSearch = Get-CalibratedPoint $Calibration "memberSearchInput" $MainFrame
  if (-not $panelProbe.LooksLikeGroup -and -not $calibratedSearch) {
    Write-TraceStage ("refuse-member-search-page-not-group " + $Prefix + " confidence=" + $panelProbe.Confidence + " memberSearchY=" + $panelProbe.MemberSearchY + " score=" + ($panelProbe.Score | ConvertTo-Json -Compress))
    return $null
  }
  if ($calibratedSearch) {
    $script:CalibrationUsed = $true
    if (-not $panelProbe.LooksLikeGroup) {
      Write-TraceStage ("member-search-using-calibrated-point-without-visual-group-proof " + $Prefix + " confidence=" + $panelProbe.Confidence)
    }
    Write-TraceStage ("click-member-search-calibrated " + $Prefix + " x=" + $calibratedSearch.X + " y=" + $calibratedSearch.Y)
    Click-At $calibratedSearch.X $calibratedSearch.Y
  } else {
    $memberSearchY = [int]$panelProbe.MemberSearchY
    if ($memberSearchY -gt 0) {
      $candidateSearchYs = @($memberSearchY, ($memberSearchY + 8), ($memberSearchY - 8))
      $primarySearchY = $memberSearchY
    } else {
      $candidateSearchYs = @(($mainTop + 258), ($mainTop + 270), ($mainTop + 246), ($mainTop + 282))
      $primarySearchY = $mainTop + 258
    }
    foreach ($rawSearchIconY in $candidateSearchYs) {
      $searchIconY = [int]([Math]::Max($mainTop + 230, [Math]::Min($mainTop + 330, [int]$rawSearchIconY)))
      foreach ($searchIconX in @(($searchRight - 31), ($searchRight - 48))) {
        Write-TraceStage ("click-member-search " + $Prefix + " x=" + $searchIconX + " y=" + $searchIconY)
        Click-AtFast $searchIconX $searchIconY
      }
      $searchInputX = [int]($searchRight - 145)
      Write-TraceStage ("focus-member-search-input " + $Prefix + " x=" + $searchInputX + " y=" + $searchIconY)
      Click-AtFast $searchInputX $searchIconY
    }
    Start-Sleep -Milliseconds 400
    $primarySearchY = [int]([Math]::Max($mainTop + 230, [Math]::Min($mainTop + 330, [int]$primarySearchY)))
    $searchInputX = [int]($searchRight - 145)
    Write-TraceStage ("focus-member-search-input-final " + $Prefix + " x=" + $searchInputX + " y=" + $primarySearchY)
    Click-At $searchInputX $primarySearchY
  }
  Write-TraceStage ("type-target-qq " + $Prefix)
  Press-CtrlA-Backspace
  if (-not (Type-Digits $TargetQQ)) {
    Paste-Text $TargetQQ
  }
  Start-Sleep -Seconds 2
  Write-TraceStage ("close-interfering-popups " + $Prefix)
  Close-ProfilePopups
  Focus-Maximized $MainFrame.Handle
  $fresh = Get-MainQQWindow
  $script:MainHandleValue = $fresh.HandleValue
  $memberResultShot = Save-Shot $fresh ($Prefix + "-member-search-result.png")
  [void]$shots.Add($memberResultShot)
  $script:LastGroupLikeShot = $memberResultShot
  Write-TraceStage ("open-search-result-private-chat-context-menu " + $Prefix)
  $privateOpened = Open-SearchResultPrivateChat $fresh $SearchResultBaseY $Calibration $Prefix
  if ($privateOpened) {
    return $privateOpened
  }

  Write-TraceStage ("context-menu-private-chat-not-opened-try-profile " + $Prefix)
  $calibratedResult = Get-CalibratedPoint $Calibration "searchResultFirst" $fresh
  if ($calibratedResult) {
    $script:CalibrationUsed = $true
    Write-TraceStage ("click-search-result-calibrated " + $Prefix + " x=" + $calibratedResult.X + " y=" + $calibratedResult.Y)
    Click-At $calibratedResult.X $calibratedResult.Y
    $foundProfile = Wait-ForProfileQuick 2800
    if (-not $foundProfile) {
      DoubleClick-At $calibratedResult.X $calibratedResult.Y
      $foundProfile = Wait-ForProfileQuick 3200
    }
  } else {
    $foundProfile = Open-SearchResultProfile $fresh $SearchResultBaseY
  }
  if (-not $foundProfile) {
    [void]$shots.Add((Save-Shot $fresh ($Prefix + "-after-result-clicks.png")))
  }
  return $foundProfile
}

$shots = New-Object System.Collections.ArrayList
$steps = New-Object System.Collections.ArrayList

if ($Mode -eq "calibrate") {
  Invoke-Calibration
  return
}

$script:Calibration = Read-Calibration $CalibrationFile
$script:CalibrationUsed = $false

Write-TraceStage "close-profile-popups"
Close-ProfilePopups
Write-TraceStage "get-main-window"
$main = Get-MainQQWindow
$script:MainHandleValue = $main.HandleValue
Write-TraceStage ("main-window=" + ($main | ConvertTo-Json -Compress))
Write-TraceStage "focus-main-window"
Focus-Maximized $main.Handle
Write-TraceStage "press-escape"
Press-Escape
Press-Escape
$main = Get-MainQQWindow
$script:MainHandleValue = $main.HandleValue
[void]$shots.Add((Save-Shot $main "01-maximized-start.png"))
[void]$steps.Add("maximized")
Write-TraceStage "maximized"

$profile = $null
$usedMemberSearch = $false
if ($TargetQQ) {
  $usedMemberSearch = $true
  $currentPageProbe = Test-CurrentPageLooksLikeGroup $main
  $groupScore = $currentPageProbe.Score
  if ($currentPageProbe.LooksLikeGroup) {
    Write-TraceStage ("try-current-member-search-before-click-group confidence=" + $currentPageProbe.Confidence)
    $profile = Invoke-MemberSearchFromPage $main "02-current" $script:Calibration
    if ($profile) {
      Write-TraceStage "current-member-search-profile-opened"
      [void]$steps.Add("current-member-search-ok")
    } else {
      Write-TraceStage "current-member-search-no-profile"
    }
  } else {
    Write-TraceStage "skip-current-member-search-page-not-group"
  }

  if (-not $profile) {
    Press-Escape
    Press-CtrlA-Backspace
    Close-ProfilePopups
    $main = Get-MainQQWindow
    $script:MainHandleValue = $main.HandleValue
    Focus-Maximized $main.Handle

    $calibratedGroup = Get-CalibratedPoint $script:Calibration "pinnedGroup" $main
    if ($calibratedGroup) {
      $script:CalibrationUsed = $true
      $groupX = $calibratedGroup.X
      $groupY = $calibratedGroup.Y
    } else {
      $groupX = $main.Left + [int]([Math]::Min(230, [Math]::Max(140, $main.Width * 0.07)))
      $groupY = $main.Top + $GroupBaseY + (($GroupRow - 1) * 95)
    }
    $calibratedSearchForRoute = Get-CalibratedPoint $script:Calibration "memberSearchInput" $main
    $hasCalibratedRoute = [bool]($calibratedGroup -and $calibratedSearchForRoute)
    if ($hasCalibratedRoute) {
      Write-TraceStage "calibrated-group-route-available"
    }
    $groupOpened = $false
    $lastGroupOpenProbe = $null
    for ($groupClickAttempt = 1; $groupClickAttempt -le 2 -and -not $groupOpened; $groupClickAttempt++) {
      Write-TraceStage ("click-pinned-group-after-current-search-failed attempt=" + $groupClickAttempt + " x=" + $groupX + " y=" + $groupY)
      Click-At $groupX $groupY
      Start-Sleep -Milliseconds 1200
      $main = Get-MainQQWindow
      $script:MainHandleValue = $main.HandleValue
      Focus-Maximized $main.Handle
      $main = Get-MainQQWindow
      $script:MainHandleValue = $main.HandleValue
      $lastGroupOpenProbe = Wait-ForStableGroupPage $main 4 ("03-after-pinned-group-click-" + $groupClickAttempt) 2
      $groupScore = $lastGroupOpenProbe.Score
      $groupOpened = [bool]$lastGroupOpenProbe.Stable
      if (-not $groupOpened -and $hasCalibratedRoute) {
        Write-TraceStage ("pinned-group-visual-proof-missing-but-calibrated-route-will-continue attempt=" + $groupClickAttempt + " confidence=" + $lastGroupOpenProbe.Confidence + " memberSearchY=" + $lastGroupOpenProbe.MemberSearchY + " score=" + ($groupScore | ConvertTo-Json -Compress))
        $groupOpened = $true
      } else {
        Write-TraceStage ("pinned-group-open-probe attempt=" + $groupClickAttempt + " stable=" + $lastGroupOpenProbe.Stable + " confidence=" + $lastGroupOpenProbe.Confidence + " memberSearchY=" + $lastGroupOpenProbe.MemberSearchY + " score=" + ($groupScore | ConvertTo-Json -Compress))
      }
      if (-not $groupOpened -and $groupClickAttempt -lt 2) {
        Press-Escape
        Close-ProfilePopups
        $main = Get-MainQQWindow
        $script:MainHandleValue = $main.HandleValue
        Focus-Maximized $main.Handle
      }
    }
    if (-not $groupOpened) {
      throw ("pinned group was not opened after two clicks; probe=" + ($lastGroupOpenProbe | ConvertTo-Json -Compress))
    }
    [void]$steps.Add("group-open-ok")
    $profile = Invoke-MemberSearchFromPage $main "04-pinned" $script:Calibration
  }
} else {
  Write-TraceStage "check-group-panel-probe"
  $groupScore = Wait-ForGroupPanel $main $WaitSeconds
  Write-TraceStage ("group-panel-score=" + ($groupScore | ConvertTo-Json -Compress))
  [void]$steps.Add("group-panel-ok")
  # Clear any text in the group editor before touching the visible member list.
  $editorX = $main.Left + [int]($main.Width * 0.40)
  $editorY = $main.Top + $main.Height - 235
  Click-At $editorX $editorY
  Press-CtrlA-Backspace
}

if (-not $TargetQQ) {
  $memberX = $main.Left + $main.Width - 240
  $memberY = $main.Top + $MemberBaseY + (($MemberRow - 1) * 49)
  Click-At $memberX $memberY
  $profile = Try-WaitForProfile $WaitSeconds
}

$privateChatOpenedBySearch = $false
if ($profile -and $profile.PSObject.Properties["PrivateChatOpened"] -and [bool]$profile.PrivateChatOpened) {
  $privateChatOpenedBySearch = $true
  Write-TraceStage ("private-chat-opened-result via=" + $profile.Via)
  [void]$steps.Add("private-chat-opened-by-search-context-menu")
}

if (-not $profile -and -not $privateChatOpenedBySearch) {
  Write-TraceStage "wait-profile"
  $profileWaitSeconds = $WaitSeconds
  if ($TargetQQ) {
    if (Get-StoredCalibrationPoint $script:Calibration "profileSendButton") {
      $profileWaitSeconds = 4
    } else {
      $profileWaitSeconds = [Math]::Min([Math]::Max($WaitSeconds, 12), 15)
    }
  }
  Write-TraceStage ("wait-profile-seconds=" + $profileWaitSeconds)
  $profile = Try-WaitForProfile $profileWaitSeconds
}

$profileOcr = ""
if ($privateChatOpenedBySearch) {
  $main = Get-MainQQWindow
  $script:MainHandleValue = $main.HandleValue
  Focus-Maximized $main.Handle
  $main = Get-MainQQWindow
  $script:MainHandleValue = $main.HandleValue
  $privateBeforeSendShot = Save-Shot $main "05-private-chat-before-send.png"
  [void]$shots.Add($privateBeforeSendShot)
  Write-TraceStage "assert-private-chat-context-menu"
  $privateScore = Assert-PrivateChat $main $privateBeforeSendShot $TargetQQ
  [void]$steps.Add("private-chat-ok")
} elseif ($profile) {
  Write-TraceStage ("profile-window=" + ($profile | ConvertTo-Json -Compress))
  $profileShot = Save-Shot $profile "04-profile-card.png"
  [void]$shots.Add($profileShot)
  Write-TraceStage "ocr-profile"
  $profileOcr = Assert-ProfileQQ $profileShot $TargetQQ
  [void]$steps.Add("profile-ok")

  $calibratedSend = Get-CalibratedPoint $script:Calibration "profileSendButton" $profile
  if ($calibratedSend) {
    $script:CalibrationUsed = $true
    $sendX = $calibratedSend.X
    $sendY = $calibratedSend.Y
  } else {
    $sendX = $profile.Left + [int]($profile.Width * 0.73)
    $sendY = $profile.Top + $profile.Height - 62
  }
  Write-TraceStage ("click-send-button x=" + $sendX + " y=" + $sendY)
} else {
  Write-TraceStage "profile-not-detected-try-calibrated-send-button"
  try {
    $main = Get-MainQQWindow
    $script:MainHandleValue = $main.HandleValue
    [void]$shots.Add((Save-Shot $main "04-profile-not-detected-before-calibrated-send.png"))
  } catch {}
  $storedSend = Get-StoredCalibrationPoint $script:Calibration "profileSendButton"
  if (-not $storedSend) {
    throw "profile card was not detected and no calibrated send button is available"
  }
  if (-not (Test-CalibrationMainFrame $script:Calibration $main)) {
    throw "profile card was not detected and calibrated send button is not valid for current QQ window size; please recalibrate"
  }
  if ($TargetQQ) {
    $storedFrame = $storedSend.Frame
    if (-not $storedFrame -or $null -eq $storedFrame.left -or $null -eq $storedFrame.top -or $null -eq $storedFrame.width -or $null -eq $storedFrame.height) {
      throw "profile card was not detected and calibrated profile frame is missing; please recalibrate"
    }
    $profileApprox = [pscustomobject]@{
      Left = [int]$storedFrame.left
      Top = [int]$storedFrame.top
      Width = [int]$storedFrame.width
      Height = [int]$storedFrame.height
    }
    $profileApproxShot = Save-Shot $profileApprox "04-profile-card-stored-calibration-area.png"
    [void]$shots.Add($profileApproxShot)
    Write-TraceStage "ocr-profile-stored-calibration-area"
    try {
      $profileOcr = Assert-ProfileQQ $profileApproxShot $TargetQQ
      [void]$steps.Add("profile-calibrated-area-ok")
    } catch {
      throw ("profile card was not detected and calibrated profile area did not show target QQ; " + $_.Exception.Message)
    }
  }
  $script:CalibrationUsed = $true
  $sendX = $storedSend.X
  $sendY = $storedSend.Y
  Write-TraceStage ("click-send-button-stored-calibration x=" + $sendX + " y=" + $sendY)
}

if (-not $privateChatOpenedBySearch) {
  Click-At $sendX $sendY
  Start-Sleep -Seconds 1
  $main = Get-MainQQWindow
  $script:MainHandleValue = $main.HandleValue
  Focus-Maximized $main.Handle
  $main = Get-MainQQWindow
  $script:MainHandleValue = $main.HandleValue
  $privateBeforeSendShot = Save-Shot $main "05-private-chat-before-send.png"
  [void]$shots.Add($privateBeforeSendShot)
  Write-TraceStage "assert-private-chat"
  $privateScore = Assert-PrivateChat $main $privateBeforeSendShot $TargetQQ
  [void]$steps.Add("private-chat-ok")
}

$sent = $false
if ($Mode -eq "send") {
  $calibratedPrivateInput = Get-CalibratedPoint $script:Calibration "privateChatInput" $main
  if ($calibratedPrivateInput) {
    $script:CalibrationUsed = $true
    $privateEditorX = $calibratedPrivateInput.X
    $privateEditorY = $calibratedPrivateInput.Y
  } else {
    $privateEditorX = $main.Left + [int]($main.Width * 0.40)
    $privateEditorY = $main.Top + $main.Height - 235
  }
  Write-TraceStage ("click-private-editor x=" + $privateEditorX + " y=" + $privateEditorY)
  Click-At $privateEditorX $privateEditorY
  Press-CtrlA-Backspace
  if ($MessageKind -eq "image") {
    Write-TraceStage "paste-warmup-image"
    Paste-Image $ImagePath
  } else {
    Write-TraceStage "paste-warmup-message"
    Paste-Text $Message
  }
  $main = Get-MainQQWindow
  $script:MainHandleValue = $main.HandleValue
  $privateBeforeEnterShot = Save-Shot $main "05-private-chat-before-enter.png"
  [void]$shots.Add($privateBeforeEnterShot)
  Write-TraceStage "assert-private-chat-before-enter"
  [void](Assert-PrivateChat $main $privateBeforeEnterShot $TargetQQ)
  Write-TraceStage "press-enter"
  Press-Enter
  Start-Sleep -Seconds 1
  $sent = $true
  [void]$shots.Add((Save-Shot $main "06-private-chat-after-send.png"))
  [void]$steps.Add("sent")
  Write-TraceStage "sent"
}

$profileResult = $null
if ($profile) {
  $profileResult = @{
    handle = $profile.HandleValue
    left = $profile.Left
    top = $profile.Top
    width = $profile.Width
    height = $profile.Height
  }
}

$result = [ordered]@{
  mode = $Mode
  sent = $sent
  targetQQ = $TargetQQ
  messageKind = $MessageKind
  message = $Message
  imagePath = $ImagePath
  groupRow = $GroupRow
  memberRow = $MemberRow
  groupBaseY = $GroupBaseY
  memberBaseY = $MemberBaseY
  searchResultBaseY = $SearchResultBaseY
  usedMemberSearch = $usedMemberSearch
  calibrationFile = $CalibrationFile
  calibrationUsed = $script:CalibrationUsed
  profileOcr = $profileOcr
  main = @{
    handle = $main.HandleValue
    left = $main.Left
    top = $main.Top
    width = $main.Width
    height = $main.Height
  }
  profile = $profileResult
  groupPanelScore = $groupScore
  privateGuardScore = $privateScore
  steps = @($steps)
  shots = @($shots)
  outDir = $OutDir
  foreground = [NmfStable]::GetForegroundWindow().ToInt64()
}

($result | ConvertTo-Json -Depth 8) | Set-Content -Path (Join-Path $OutDir "result.json") -Encoding UTF8
$result | ConvertTo-Json -Depth 8
