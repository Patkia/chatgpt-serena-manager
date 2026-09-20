param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$TunnelClient,
    [Parameter(Mandatory = $true)][string]$Profile,
    [Parameter(Mandatory = $true)][string]$CredentialFile,
    [Parameter(Mandatory = $true)][int]$SerenaPort,
    [Parameter(Mandatory = $true)][int]$TunnelPort,
    [Parameter(Mandatory = $true)][string]$Label,
    [string]$SerenaExtraArgs = '',
    [switch]$DebugMode
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$logRoot = Join-Path $root 'logs'
$launcherLog = Join-Path $logRoot 'launcher.log'
$serenaLog = Join-Path $logRoot 'serena.log'
$tunnelLog = Join-Path $logRoot 'tunnel.log'
$tunnelErrorLog = Join-Path $logRoot 'tunnel.stderr.log'
$tunnelProfileDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'tunnel-client'
$tunnelProfilePath = Join-Path $tunnelProfileDir ($Profile + '.yaml')
$serenaCommand = Get-Command 'serena.exe' -ErrorAction SilentlyContinue
if (-not $serenaCommand) { $serenaCommand = Get-Command 'serena' -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

function Write-Event([string]$Message) {
    $line = "{0:o} [{1}] {2}" -f (Get-Date), $Label, $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Rotate-Log([string]$Path) {
    if ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -gt 10MB) {
        $two = "$Path.2"; $one = "$Path.1"
        if (Test-Path -LiteralPath $two) { Remove-Item -LiteralPath $two -Force }
        if (Test-Path -LiteralPath $one) { Move-Item -LiteralPath $one -Destination $two -Force }
        Move-Item -LiteralPath $Path -Destination $one -Force
    }
}

function Get-PortProcesses([int]$Port) {
    $rows = foreach ($connection in @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction SilentlyContinue
        if ($p) { [PSCustomObject]@{ Id = [int]$p.ProcessId; Name = [string]$p.Name; CommandLine = [string]$p.CommandLine } }
    }
    @($rows | Sort-Object Id -Unique)
}

function Test-Serena($p) {
    $line = [string]$p.CommandLine
    return $line -match '(?i)serena' -and $line -match '(?i)\bstart-mcp-server\b' -and
        $line -match ('(?i)' + [regex]::Escape($Project)) -and $line -match ('(?i)--port\s+' + $SerenaPort + '(?:\s|$)')
}
function Test-Tunnel($p) {
    $line = [string]$p.CommandLine
    return $line -match '(?i)tunnel-client(?:\.exe)?' -and $line -match '(?i)\brun\b' -and
        $line -match ('(?i)(?:--profile\s+["'']?' + [regex]::Escape($Profile) + '["'']?|profile[=:]\s*' + [regex]::Escape($Profile) + ')(?:\s|$)')
}
function Test-Http([string]$Url) { try { $r = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop; return $r.StatusCode -ge 200 -and $r.StatusCode -lt 500 } catch { return $null -ne $_.Exception.Response } }
function Test-Ready([string]$Url) { try { $r = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop; return $r.StatusCode -eq 200 -and ([string]$r.Content).Trim().ToLowerInvariant() -eq 'ready' } catch { return $false } }
function Wait-Until([scriptblock]$Check, [int]$Seconds) { $until = (Get-Date).AddSeconds($Seconds); do { if (& $Check) { return $true }; Start-Sleep -Seconds 1 } while ((Get-Date) -lt $until); return $false }

Rotate-Log $launcherLog; Rotate-Log $serenaLog; Rotate-Log $tunnelLog; Rotate-Log $tunnelErrorLog
Write-Event $(if ($DebugMode) { 'DEBUG START requested' } else { 'START requested' })
if (-not (Test-Path -LiteralPath $Project -PathType Container)) { Write-Event 'ERROR project path not found'; exit 2 }
if (-not (Test-Path -LiteralPath $TunnelClient -PathType Leaf)) { Write-Event 'ERROR tunnel client not found'; exit 2 }
if (-not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) { Write-Event 'ERROR DPAPI credential file not found'; exit 2 }
if (-not $serenaCommand) { Write-Event 'ERROR Serena command not found on PATH'; exit 2 }

$serenaListeners = @(Get-PortProcesses $SerenaPort)
$serenaOwner = @($serenaListeners | Where-Object { Test-Serena $_ })
$serenaReady = $false
if ($serenaListeners.Count -eq 0) {
    $serenaArgs = $SerenaExtraArgs
    if (-not $DebugMode -and $serenaArgs -notmatch '(?i)--enable-web-dashboard') {
        $serenaArgs = '--enable-web-dashboard false --open-web-dashboard false --enable-gui-log-window false ' + $serenaArgs
    }
    $serenaExecutable = [string]$serenaCommand.Source
    $command = "& '$serenaExecutable' start-mcp-server --project '$Project' --transport streamable-http --host 127.0.0.1 --port $SerenaPort $serenaArgs"
    if ($DebugMode) { Start-Process powershell.exe -WindowStyle Normal -ArgumentList '-NoLogo','-NoProfile','-NoExit','-Command', $command | Out-Null }
    else {
        $serenaArguments = "start-mcp-server --project `"$Project`" --transport streamable-http --host 127.0.0.1 --port $SerenaPort $serenaArgs"
        $serenaProcess = Start-Process -FilePath $serenaExecutable -WindowStyle Hidden -ArgumentList $serenaArguments -RedirectStandardOutput $serenaLog -PassThru
        Write-Event "Serena process started pid=$($serenaProcess.Id)"
    }
    Write-Event "waiting for Serena MCP port $SerenaPort"
    $serenaReady = Wait-Until { (@(Get-PortProcesses $SerenaPort | Where-Object { Test-Serena $_ }).Count -gt 0) -and (Test-Http "http://127.0.0.1:$SerenaPort/mcp") } 30
} elseif ($serenaOwner.Count -eq $serenaListeners.Count) { $serenaReady = Test-Http "http://127.0.0.1:$SerenaPort/mcp" } else { Write-Event "ERROR MCP port $SerenaPort has an unexpected owner" }
if (-not $serenaReady) { Write-Event 'ERROR Serena is not ready'; exit 1 }

$secureKey = ConvertTo-SecureString (Get-Content -LiteralPath $CredentialFile -Raw)
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:CONTROL_PLANE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $tunnelListeners = @(Get-PortProcesses $TunnelPort)
    $tunnelOwner = @($tunnelListeners | Where-Object { Test-Tunnel $_ })
    $tunnelReady = $false
    if ($tunnelListeners.Count -eq 0) {
        $command = "& '$TunnelClient' run --profile '$Profile' --profile-dir '$tunnelProfileDir'"
        if (-not (Test-Path -LiteralPath $tunnelProfilePath -PathType Leaf)) { Write-Event "ERROR tunnel profile file not found: $tunnelProfilePath"; exit 2 }
        Write-Event "tunnel profile path=$tunnelProfilePath"
        if ($DebugMode) { Start-Process powershell.exe -WindowStyle Normal -ArgumentList '-NoLogo','-NoProfile','-NoExit','-Command', $command | Out-Null }
        else {
        $tunnelArguments = "run --profile `"$Profile`" --profile-dir `"$tunnelProfileDir`""
        $tunnelProcess = Start-Process -FilePath $TunnelClient -WindowStyle Hidden -ArgumentList $tunnelArguments -RedirectStandardOutput $tunnelLog -RedirectStandardError $tunnelErrorLog -PassThru
        Write-Event "tunnel process started pid=$($tunnelProcess.Id) profile=$Profile command=$TunnelClient run --profile $Profile --profile-dir $tunnelProfileDir"
        }
        Write-Event "waiting for tunnel profile $Profile on health port $TunnelPort"
        $tunnelExitLogged = $false
        $tunnelReady = Wait-Until {
            if ($tunnelProcess -and $tunnelProcess.HasExited) {
                if (-not $tunnelExitLogged) { Write-Event "tunnel process exited pid=$($tunnelProcess.Id) code=$($tunnelProcess.ExitCode)"; $tunnelExitLogged = $true }
                return $false
            }
            Test-Ready "http://127.0.0.1:$TunnelPort/readyz"
        } 30
    } elseif ($tunnelOwner.Count -eq $tunnelListeners.Count) { $tunnelReady = Test-Ready "http://127.0.0.1:$TunnelPort/readyz" } else { Write-Event "ERROR health port $TunnelPort has an unexpected owner" }
} finally { if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }; $env:CONTROL_PLANE_API_KEY = $null }
if (-not $tunnelReady) {
    if ($tunnelProcess -and -not $tunnelProcess.HasExited) {
        Write-Event "ERROR tunnel not ready; stopping child pid=$($tunnelProcess.Id)"
        Stop-Process -Id $tunnelProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Event 'ERROR tunnel is not ready'
    exit 1
}
Write-Event $(if ($DebugMode) { 'DEBUG START ready' } else { 'START ready' })
exit 0
