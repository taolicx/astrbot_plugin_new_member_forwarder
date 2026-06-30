param(
  [ValidateSet("probe", "send")]
  [string]$Mode = "probe",
  [int]$GroupRow = 1,
  [int]$MemberRow = 3,
  [int]$GroupBaseY = 150,
  [int]$MemberBaseY = 322,
  [int]$SearchResultBaseY = 322,
  [string]$TargetQQ = "",
  [string]$Message = "",
  [int]$WaitSeconds = 8,
  [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $OutDir) {
  $OutDir = Join-Path $PSScriptRoot ("stable-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
try {
  Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
  Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop
  $script:UiaAvailable = $true
} catch {
  $script:UiaAvailable = $false
}

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
}
"@

[NmfStable]::SetProcessDPIAware() | Out-Null

$script:ProfileTitle = -join ([char[]](0x8d44, 0x6599, 0x5361))
if (-not $Message) {
  $Message = -join ([char[]](0x6b22, 0x8fce, 0x8fdb, 0x7fa4))
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
  $windows = Find-QQWindows
  $main = $windows | Where-Object { $_.Title -eq "QQ" -and $_.Width -ge 800 -and $_.Height -ge 600 } | Select-Object -First 1
  if (-not $main) {
    $main = $windows | Where-Object { $_.Width -ge 800 -and $_.Height -ge 600 } | Select-Object -First 1
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

function Paste-Text([string]$Text) {
  [System.Windows.Forms.Clipboard]::SetText($Text)
  Start-Sleep -Milliseconds 120
  [NmfStable]::keybd_event(0x11, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x56, 0, 0, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x56, 0, 2, [UIntPtr]::Zero)
  [NmfStable]::keybd_event(0x11, 0, 2, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 300
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

function Assert-PrivateChat($Frame) {
  $score = Get-GroupPanelScore $Frame
  if ($score.GroupPanelDetected -or $score.Ratio -gt 0.12) {
    throw ("private chat guard refused to send; group panel still visible; score=" + ($score | ConvertTo-Json -Compress))
  }
  $score
}

function Close-ProfilePopups {
  foreach ($popup in @(Find-QQWindows | Where-Object { $_.Title -eq $script:ProfileTitle })) {
    [NmfStable]::PostMessage($popup.Handle, 0x0010, [UIntPtr]::Zero, [UIntPtr]::Zero) | Out-Null
  }
  Start-Sleep -Milliseconds 400
}

function Wait-ForProfile([int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    $profile = Find-QQWindows | Where-Object {
      $_.Title -eq $script:ProfileTitle -and $_.Visible -and $_.Left -ge 0 -and $_.Top -ge 0 -and $_.Width -gt 300 -and $_.Height -gt 300
    } | Select-Object -First 1
    if ($profile) {
      return $profile
    }
    Start-Sleep -Milliseconds 500
  }
  throw "profile card was not detected"
}

function Try-WaitForProfile([int]$Seconds) {
  try {
    return Wait-ForProfile $Seconds
  } catch {
    return $null
  }
}

function Open-SearchResultProfile($MainFrame, [int]$BaseY) {
  $rowY = $MainFrame.Top + $BaseY
  $avatarX = $MainFrame.Left + $MainFrame.Width - 240
  $nameX = $MainFrame.Left + $MainFrame.Width - 185

  Click-At $avatarX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }

  Click-At $nameX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }

  DoubleClick-At $avatarX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }

  DoubleClick-At $nameX $rowY
  $profile = Try-WaitForProfile 2
  if ($profile) { return $profile }

  return $null
}

$shots = New-Object System.Collections.ArrayList
$steps = New-Object System.Collections.ArrayList

Close-ProfilePopups
$main = Get-MainQQWindow
Focus-Maximized $main.Handle
Press-Escape
Press-Escape
$main = Get-MainQQWindow
[void]$shots.Add((Save-Shot $main "01-maximized-start.png"))
[void]$steps.Add("maximized")

# Conversation rows are stable after the target group is pinned.
$groupX = $main.Left + [int]([Math]::Min(360, [Math]::Max(220, $main.Width * 0.16)))
$groupY = $main.Top + $GroupBaseY + (($GroupRow - 1) * 95)
Click-At $groupX $groupY
Start-Sleep -Milliseconds 900
$main = Get-MainQQWindow
[void]$shots.Add((Save-Shot $main "02-after-group-click.png"))
if ($TargetQQ) {
  $groupScore = Wait-ForGroupPanel $main $WaitSeconds
  [void]$steps.Add("group-panel-ok")
} else {
  $groupScore = Wait-ForGroupPanel $main $WaitSeconds
  [void]$steps.Add("group-panel-ok")
  # Clear any text in the group editor before touching the visible member list.
  $editorX = $main.Left + [int]($main.Width * 0.40)
  $editorY = $main.Top + $main.Height - 235
  Click-At $editorX $editorY
  Press-CtrlA-Backspace
}

$usedMemberSearch = $false
if ($TargetQQ) {
  $usedMemberSearch = $true
  $searchIconX = $main.Left + $main.Width - 31
  $searchIconY = $main.Top + 264
  Click-At $searchIconX $searchIconY
  Start-Sleep -Milliseconds 500
  Press-CtrlA-Backspace
  Paste-Text $TargetQQ
  Start-Sleep -Seconds 1
  [void]$shots.Add((Save-Shot $main "03-member-search-result.png"))
  $profile = Open-SearchResultProfile $main $SearchResultBaseY
  if (-not $profile) {
    [void]$shots.Add((Save-Shot $main "03b-after-result-clicks.png"))
  }
} else {
  $memberX = $main.Left + $main.Width - 240
  $memberY = $main.Top + $MemberBaseY + (($MemberRow - 1) * 49)
  Click-At $memberX $memberY
  $profile = Try-WaitForProfile $WaitSeconds
}
if (-not $profile) {
  $profile = Wait-ForProfile $WaitSeconds
}
$profileShot = Save-Shot $profile "04-profile-card.png"
[void]$shots.Add($profileShot)
$profileOcr = Assert-ProfileQQ $profileShot $TargetQQ
[void]$steps.Add("profile-ok")

$sendX = $profile.Left + [int]($profile.Width * 0.73)
$sendY = $profile.Top + $profile.Height - 62
Click-At $sendX $sendY
Start-Sleep -Seconds 1
$main = Get-MainQQWindow
Focus-Maximized $main.Handle
$main = Get-MainQQWindow
[void]$shots.Add((Save-Shot $main "05-private-chat-before-send.png"))
$privateScore = Assert-PrivateChat $main
[void]$steps.Add("private-chat-ok")

$sent = $false
if ($Mode -eq "send") {
  $privateEditorX = $main.Left + [int]($main.Width * 0.40)
  $privateEditorY = $main.Top + $main.Height - 235
  Click-At $privateEditorX $privateEditorY
  Press-CtrlA-Backspace
  Paste-Text $Message
  Press-Enter
  Start-Sleep -Seconds 1
  $sent = $true
  [void]$shots.Add((Save-Shot $main "06-private-chat-after-send.png"))
  [void]$steps.Add("sent")
}

$result = [ordered]@{
  mode = $Mode
  sent = $sent
  targetQQ = $TargetQQ
  message = $Message
  groupRow = $GroupRow
  memberRow = $MemberRow
  groupBaseY = $GroupBaseY
  memberBaseY = $MemberBaseY
  searchResultBaseY = $SearchResultBaseY
  usedMemberSearch = $usedMemberSearch
  profileOcr = $profileOcr
  main = @{
    handle = $main.HandleValue
    left = $main.Left
    top = $main.Top
    width = $main.Width
    height = $main.Height
  }
  profile = @{
    handle = $profile.HandleValue
    left = $profile.Left
    top = $profile.Top
    width = $profile.Width
    height = $profile.Height
  }
  groupPanelScore = $groupScore
  privateGuardScore = $privateScore
  steps = @($steps)
  shots = @($shots)
  outDir = $OutDir
  foreground = [NmfStable]::GetForegroundWindow().ToInt64()
}

($result | ConvertTo-Json -Depth 8) | Set-Content -Path (Join-Path $OutDir "result.json") -Encoding UTF8
$result | ConvertTo-Json -Depth 8
