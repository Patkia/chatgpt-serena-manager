param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Profile,
    [Parameter(Mandatory = $true)][int]$SerenaPort,
    [Parameter(Mandatory = $true)][int]$TunnelPort
)

$ErrorActionPreference = 'Stop'

function Get-ProcessById([int]$ProcessId) {
    Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Get-PortProcesses([int]$Port) {
    $result = foreach ($connection in @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        $process = Get-ProcessById $connection.OwningProcess
        if ($process) {
            [PSCustomObject]@{ Id = [int]$process.ProcessId; ParentId = [int]$process.ParentProcessId; Name = [string]$process.Name; CommandLine = [string]$process.CommandLine }
        }
    }
    @($result | Sort-Object Id -Unique)
}

function Test-SerenaProcess($Process) {
    $line = [string]$Process.CommandLine
    return $line -match '(?i)serena' -and $line -match '(?i)\bstart-mcp-server\b' -and
        $line -match ('(?i)' + [regex]::Escape($Project)) -and
        $line -match ('(?i)--port\s+' + $SerenaPort + '(?:\s|$)')
}

function Test-TunnelProcess($Process) {
    $line = [string]$Process.CommandLine
    return $line -match '(?i)tunnel-client(?:\.exe)?' -and $line -match '(?i)\brun\b' -and
        $line -match ('(?i)(?:--profile\s+["'']?' + [regex]::Escape($Profile) + '["'']?|profile[=:]\s*' + [regex]::Escape($Profile) + ')(?:\s|$)')
}

function Get-VerifiedChain([int]$ProcessId, [scriptblock]$Matcher) {
    $verified = @()
    $cursor = $ProcessId
    while ($cursor -gt 0) {
        $process = Get-ProcessById $cursor
        if (-not $process) { break }
        $item = [PSCustomObject]@{ Id = [int]$process.ProcessId; ParentId = [int]$process.ParentProcessId; Name = [string]$process.Name; CommandLine = [string]$process.CommandLine }
        if (-not (& $Matcher $item)) { break }
        $verified += $item
        $cursor = $item.ParentId
    }
    @($verified)
}

function Stop-ExactService([string]$Label, [int]$Port, [scriptblock]$Matcher) {
    $listeners = @(Get-PortProcesses $Port)
    $unexpected = @($listeners | Where-Object { -not (& $Matcher $_) })
    if ($unexpected.Count -gt 0) {
        $details = ($unexpected | ForEach-Object { "PID $($_.Id) ($($_.Name))" }) -join ', '
        Write-Host "$Label`: PORT OCCUPIED BY UNEXPECTED PROCESS - $details"
        return [PSCustomObject]@{ Label = $Label; Result = 'CONFLICT'; PortFree = $false }
    }

    $verified = @()
    foreach ($listener in $listeners) { $verified += @(Get-VerifiedChain $listener.Id $Matcher) }
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $item = [PSCustomObject]@{ Id = [int]$process.ProcessId; ParentId = [int]$process.ParentProcessId; Name = [string]$process.Name; CommandLine = [string]$process.CommandLine }
        if (& $Matcher $item) { $verified += $item }
    }
    $verified = @($verified | Sort-Object Id -Unique)

    if ($verified.Count -eq 0) {
        Write-Host "$Label`: ALREADY STOPPED"
    } else {
        foreach ($item in $verified) {
            if (Get-Process -Id $item.Id -ErrorAction SilentlyContinue) { Stop-Process -Id $item.Id -Force -ErrorAction SilentlyContinue }
        }
        $deadline = (Get-Date).AddSeconds(12)
        do {
            $remaining = @(Get-PortProcesses $Port)
            if ($remaining.Count -eq 0) { break }
            Start-Sleep -Milliseconds 250
        } while ((Get-Date) -lt $deadline)
        if (@(Get-PortProcesses $Port).Count -eq 0) {
            Write-Host "$Label`: STOPPED"
        } else {
            Write-Host "$Label`: FAILED TO STOP"
            return [PSCustomObject]@{ Label = $Label; Result = 'FAILED'; PortFree = $false }
        }
    }

    $free = @(Get-PortProcesses $Port).Count -eq 0
    [PSCustomObject]@{ Label = $Label; Result = 'SUCCESS'; PortFree = $free }
}

$serena = Stop-ExactService 'Serena' $SerenaPort ${function:Test-SerenaProcess}
$tunnel = Stop-ExactService 'Tunnel' $TunnelPort ${function:Test-TunnelProcess}
Write-Output "MCP port $SerenaPort`: $(if ($serena.PortFree) { 'FREE' } else { 'NOT FREE' })"
Write-Output "Health port $TunnelPort`: $(if ($tunnel.PortFree) { 'FREE' } else { 'NOT FREE' })"
if ($serena.Result -eq 'SUCCESS' -and $tunnel.Result -eq 'SUCCESS' -and $serena.PortFree -and $tunnel.PortFree) { Write-Output 'DONE'; exit 0 }
Write-Output 'NOT DONE'
exit 1