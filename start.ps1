[CmdletBinding()]
param(
    [string]$UniverseWalkerPath = $env:UNIVERSE_WALKER_PATH,
    [switch]$CreateShortcut
)

$ErrorActionPreference = 'Stop'

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
        foreach ($name in 'martinwoodward_universe-walker', 'universe-walker') {
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
    Start-Process $url
}

Write-Host 'Leave the server consoles open. Press Ctrl+C in each console to stop it.'
