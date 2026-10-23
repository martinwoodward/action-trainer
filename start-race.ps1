[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$race = Join-Path $PSScriptRoot 'race'

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
    @{ Script = 'leaderboard.py'; Directory = $race; Port = 8766; Title = 'Air-time leaderboard' }
    @{ Script = 'recorder.py'; Directory = $PSScriptRoot; Port = 8765; Title = "Action Trainer $([char]0x00B7) Badger" }
)

foreach ($server in $servers) {
    $url = "http://localhost:$($server.Port)"
    if (Test-RacePage -Url $url -Title $server.Title) {
        Write-Host "$($server.Title) is already running at $url"
    }
    else {
        $process = Start-Process -FilePath $python `
            -ArgumentList @('-u', $server.Script, '--no-browser') `
            -WorkingDirectory $server.Directory -PassThru
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-RacePage -Url $url -Title $server.Title)) {
            $process.Refresh()
            if ($process.HasExited) {
                throw "$($server.Script) exited with code $($process.ExitCode). Run it in a terminal to inspect its output."
            }
            if ((Get-Date) -ge $deadline) {
                throw "$($server.Script) did not become ready at $url within 30 seconds. Check its console window."
            }
            Start-Sleep -Milliseconds 250
        }
        Write-Host "$($server.Title) started at $url (PID $($process.Id))"
    }
    Start-Process $url
}

Write-Host 'Leave the server consoles open. Press Ctrl+C in each console to stop it.'
