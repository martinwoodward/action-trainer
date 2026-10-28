[CmdletBinding()]
param(
    [string]$UniverseWalkerPath = $env:UNIVERSE_WALKER_PATH,
    [switch]$CreateShortcut,
    [switch]$ShowTaskbar
)

$ErrorActionPreference = 'Stop'

# Same as Settings > Personalization > Taskbar > "Automatically hide the taskbar".
# Applies straight away for the current user; Windows remembers it.
function Set-TaskbarAutoHide {
    param([bool]$Enabled)

    if (-not ('ActionTrainer.Taskbar' -as [type])) {
        Add-Type -Namespace ActionTrainer -Name Taskbar -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct APPBARDATA {
    public uint cbSize; public IntPtr hWnd; public uint uCallbackMessage; public uint uEdge;
    public int left, top, right, bottom; public IntPtr lParam;
}
[DllImport("shell32.dll")] public static extern UIntPtr SHAppBarMessage(uint msg, ref APPBARDATA data);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string name);
'@
    }
    $ABM_GETSTATE = 4; $ABM_SETSTATE = 10; $ABS_AUTOHIDE = 1
    $data = New-Object ActionTrainer.Taskbar+APPBARDATA
    $data.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($data)
    $data.hWnd = [ActionTrainer.Taskbar]::FindWindow('Shell_TrayWnd', $null)
    $state = [int][ActionTrainer.Taskbar]::SHAppBarMessage($ABM_GETSTATE, [ref]$data).ToUInt32()
    $state = if ($Enabled) { $state -bor $ABS_AUTOHIDE } else { $state -band -bnot $ABS_AUTOHIDE }
    $data.lParam = [IntPtr]$state
    [ActionTrainer.Taskbar]::SHAppBarMessage($ABM_SETSTATE, [ref]$data) | Out-Null
}

if ($ShowTaskbar) {
    Set-TaskbarAutoHide $false
    Write-Host 'Taskbar auto-hide turned off.'
    return
}

if ($CreateShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $folders = @(
        [Environment]::GetFolderPath('Desktop')
        [Environment]::GetFolderPath('Programs')
    )
    foreach ($folder in $folders) {
        $path = Join-Path $folder 'Action Trainer.lnk'
        $shortcut = $shell.CreateShortcut($path)
        $shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
        $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$PSCommandPath`""
        $shortcut.WorkingDirectory = $PSScriptRoot
        $shortcut.Description = 'Start the Race leaderboard, Action Trainer and Universe Walker'
        $shortcut.Save()
        Write-Host "Created $path"
    }
    return
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$race = Join-Path $PSScriptRoot 'race'

if (-not $UniverseWalkerPath) {
    # Look for a clone of martinwoodward/universe-walker (its main has the live
    # dashboard) next to this checkout, then next to the main checkout when
    # running from a git worktree.
    $parents = @(Split-Path $PSScriptRoot -Parent)
    $commonDir = & git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null
    if ($LASTEXITCODE -eq 0 -and $commonDir) {
        $parents += Split-Path (Split-Path $commonDir -Parent) -Parent
    }
    $candidates = foreach ($parent in $parents) {
        foreach ($name in 'universe-walker', 'martinwoodward_universe-walker') {
            Join-Path $parent $name
        }
    }
    $UniverseWalkerPath = $candidates |
        Where-Object { Test-Path -LiteralPath (Join-Path $_ 'dashboard\server.py') } |
        Select-Object -First 1
}
if (-not $UniverseWalkerPath -or -not (Test-Path -LiteralPath (Join-Path $UniverseWalkerPath 'dashboard\server.py'))) {
    throw 'Could not find a universe-walker clone with dashboard\server.py. Clone https://github.com/martinwoodward/universe-walker next to this repository or pass -UniverseWalkerPath.'
}
$UniverseWalkerPath = (Resolve-Path -LiteralPath $UniverseWalkerPath).Path

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Creating .venv...'
    & python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
        throw 'Could not create .venv. Install Python 3.11 or newer and make sure python is available on PATH.'
    }
}

& $python -c 'import aiohttp; import bleak; import numpy; import sklearn'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing web server dependencies...'
    & $python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not install the web server dependencies. Check the pip output above and try again.'
    }
}

function Test-RacePage {
    param([string]$Url, [string]$Title)

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
    }
    catch {
        if ($null -ne $_.Exception.Response) {
            throw
        }
        # Connection failures are expected while a server is starting.
        return $false
    }
    if ($response.Content -notmatch [regex]::Escape("<title>$Title</title>")) {
        throw "Another service is responding at $Url; stop it before starting $Title."
    }
    return $true
}

$servers = @(
    @{ Name = 'leaderboard.py'; Arguments = @('-u', 'leaderboard.py', '--no-browser'); Directory = $race
       Url = 'http://localhost:8766'; Title = 'Air-time leaderboard' }
    @{ Name = 'recorder.py'; Arguments = @('-u', 'recorder.py', '--no-browser'); Directory = $PSScriptRoot
       Url = 'http://localhost:8765'; Title = "Action Trainer $([char]0x00B7) Badger" }
    # Port 8767 because the recorder already uses the dashboard's default 8765.
    @{ Name = 'universe-walker dashboard'; Arguments = @('-u', 'dashboard\server.py', '--port', '8767')
       Directory = $UniverseWalkerPath
       Url = 'http://127.0.0.1:8767/'; Title = "Universe Walker $([char]0x00B7) live badges" }
)

foreach ($server in $servers) {
    $url = $server.Url
    if (Test-RacePage -Url $url -Title $server.Title) {
        Write-Host "$($server.Title) is already running at $url"
    }
    else {
        $process = Start-Process -FilePath $python `
            -ArgumentList $server.Arguments `
            -WorkingDirectory $server.Directory -PassThru
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-RacePage -Url $url -Title $server.Title)) {
            $process.Refresh()
            if ($process.HasExited) {
                throw "$($server.Name) exited with code $($process.ExitCode). Run it in a terminal to inspect its output."
            }
            if ((Get-Date) -ge $deadline) {
                throw "$($server.Name) did not become ready at $url within 30 seconds. Check its console window."
            }
            Start-Sleep -Milliseconds 250
        }
        Write-Host "$($server.Title) started at $url (PID $($process.Id))"
    }
}

# Open the pages once every server is up, in presentation order.
try {
    Set-TaskbarAutoHide $true
    Write-Host 'Taskbar set to auto-hide. Run .\start.ps1 -ShowTaskbar to undo.'
}
catch {
    Write-Warning "Could not auto-hide the taskbar: $($_.Exception.Message)"
}
$pages = @(
    'https://badger.github.io/simulator/'
    'http://localhost:8765/'
    'http://localhost:8767/dashboard/'
    'http://localhost:8766/'
)
foreach ($page in $pages) {
    Start-Process $page
}

Write-Host 'Leave the server consoles open. Press Ctrl+C in each console to stop it.'
