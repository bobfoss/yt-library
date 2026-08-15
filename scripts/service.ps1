<#
.SYNOPSIS
Controls the local YT Library background service on Windows.

.DESCRIPTION
This script is the source of truth for service process handling. It reads the
configured host and port, records the queue state, stops the queue cleanly,
stops the listener and its venv launcher, confirms that the port is closed,
launches .venv\Scripts\python.exe directly with a hidden window, redirects the
service streams to .codex\service-logs, verifies the listener/venv process
chain, and resumes the queue only when it was running before a restart.
Repository-scoped locking serializes mutating operations from concurrent chats.
When a call encounters an active operation, it emits a warning before waiting
and reports the lock wait in the returned result.

Mutating operations write transitions and failures to the bounded rolling
.codex\service-logs\service-control.log. The current service run writes to the
fixed service.stdout.log and service.stderr.log paths in that directory. New
launches replace the stream logs and remove legacy service-launch files.

.PARAMETER Action
status, start, restart, or stop. The default is status.

.PARAMETER TimeoutSeconds
Maximum time to wait for each queue, process, port, or health transition.

.PARAMETER Force
Allows replacement of an unresponsive YT Library listener or one whose venv
parent cannot be verified. It never permits stopping an unrelated process.

.PARAMETER Json
Writes the final result as compact JSON for machine-readable callers.

.EXAMPLE
.\scripts\service.ps1 restart

.EXAMPLE
.\scripts\service.ps1 status -Json

.NOTES
Do not replace this procedure with the Admin restart endpoint because that
endpoint inherits the current interpreter. Do not wrap the background launch
in cmd.exe, powershell.exe, or pwsh.exe; doing so can leave a visible console.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("status", "start", "restart", "stop")]
    [string]$Action = "status",

    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 30,

    [switch]$Force,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$configPath = Join-Path $repoRoot "yt_library.config.json"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$managerScript = "yt_library_manager.py"
$logDirectory = Join-Path $repoRoot ".codex\service-logs"
$controlLogPath = Join-Path $logDirectory "service-control.log"
$controlLogMaxBytes = 262144
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$repoNameBytes = [System.Text.Encoding]::UTF8.GetBytes($repoRoot.ToLowerInvariant())
$repoNameHash = [System.Security.Cryptography.SHA256]::HashData($repoNameBytes)
$repoNameToken = [System.Convert]::ToHexString($repoNameHash).Substring(0, 16)
$operationMutexName = "Local\YTLibraryServiceControl-$repoNameToken"
$operationLockWaited = $false
$operationLockWaitSeconds = 0.0

function Write-ControlLog {
    param(
        [ValidateSet("INFO", "WARN", "ERROR")]
        [string]$Level,
        [string]$Message
    )

    $timestamp = (Get-Date).ToUniversalTime().ToString("o")
    $line = "$timestamp [$Level] $Message"
    if (
        (Test-Path -LiteralPath $controlLogPath -PathType Leaf) -and
        (Get-Item -LiteralPath $controlLogPath).Length -ge $controlLogMaxBytes
    ) {
        $recentLines = Get-Content -LiteralPath $controlLogPath -Tail 500
        Set-Content -LiteralPath $controlLogPath -Value $recentLines -Encoding utf8
    }
    Add-Content -LiteralPath $controlLogPath -Value $line -Encoding utf8
    Write-Verbose $line
}

function Reset-ServiceRunLogs {
    Get-ChildItem -LiteralPath $logDirectory -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -in @(
                "service.stdout.log",
                "service.stderr.log",
                "latest-launcher-pid.txt"
            ) -or
            $_.Name -match '^(service|yt-library|ytl).*[-.](out|err|stdout|stderr)\.log$'
        } |
        Remove-Item -Force
}

function Get-ObjectValue {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$Default
    )

    if ($null -eq $InputObject) {
        return $Default
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return $Default
    }
    if ($property.Value -is [string] -and [string]::IsNullOrWhiteSpace($property.Value)) {
        return $Default
    }
    return $property.Value
}

function Get-ServiceConfiguration {
    $config = [pscustomobject]@{}
    if (Test-Path -LiteralPath $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    }

    $bindHost = [string](Get-ObjectValue $config "host" "127.0.0.1")
    $port = [int](Get-ObjectValue $config "port" 8765)
    $probeHost = if ($bindHost -in @("0.0.0.0", "::")) { "127.0.0.1" } else { $bindHost }
    $uriHost = if ($probeHost.Contains(":")) { "[$probeHost]" } else { $probeHost }

    return [pscustomobject]@{
        BindHost = $bindHost
        Port = $port
        BaseUrl = "http://${uriHost}:$port"
    }
}

$serviceConfig = Get-ServiceConfiguration

function Get-ServiceStatus {
    $timeout = [Math]::Min([Math]::Max($TimeoutSeconds, 5), 15)
    try {
        $request = @{
            Uri = "$($serviceConfig.BaseUrl)/api/admin/runtime/status"
            TimeoutSec = $timeout
        }
        return Invoke-RestMethod @request
    }
    catch {
        $responseProperty = $_.Exception.PSObject.Properties["Response"]
        $statusCode = if ($null -ne $responseProperty -and $null -ne $responseProperty.Value) {
            [int]$responseProperty.Value.StatusCode
        }
        else {
            0
        }
        if ($statusCode -ne 404) {
            return $null
        }
    }

    # Allows this script revision to replace the one older service that predates the runtime endpoint.
    try {
        return Invoke-RestMethod -Uri "$($serviceConfig.BaseUrl)/api/admin/status?queue_limit=0&include_logs=0" -TimeoutSec $timeout
    }
    catch {
        return $null
    }
}

function Get-ListenerProcessId {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $serviceConfig.Port -ErrorAction SilentlyContinue
    )
    if (-not $listeners) {
        return 0
    }

    $processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($processIds.Count -ne 1) {
        throw "Port $($serviceConfig.Port) has multiple listener processes: $($processIds -join ', ')"
    }
    return [int]$processIds[0]
}

function Get-ProcessRecord {
    param([int]$ProcessId)

    return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-YtLibraryProcess {
    param([object]$ProcessRecord)

    return $null -ne $ProcessRecord -and [string]$ProcessRecord.CommandLine -like "*$managerScript*"
}

function Get-VenvLauncherRecord {
    param([int]$ServiceProcessId)

    $current = Get-ProcessRecord $ServiceProcessId
    for ($depth = 0; $depth -lt 6 -and $null -ne $current; $depth++) {
        if (
            [string]$current.ExecutablePath -ieq $venvPython -and
            (Test-YtLibraryProcess $current)
        ) {
            return $current
        }
        $parentProcessId = [int]$current.ParentProcessId
        if ($parentProcessId -le 0 -or $parentProcessId -eq [int]$current.ProcessId) {
            break
        }
        $current = Get-ProcessRecord $parentProcessId
    }
    return $null
}

function Wait-ForCondition {
    param(
        [scriptblock]$Condition,
        [string]$FailureMessage
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw $FailureMessage
}

function Stop-QueueIfRunning {
    param([object]$Status)

    if ($null -eq $Status -or -not [bool]$Status.workerQueueRunning) {
        return
    }

    Write-ControlLog "INFO" "Requesting worker queue stop; count=$([int]$Status.workerQueueCount)."
    $request = @{
        Method = "Post"
        Uri = "$($serviceConfig.BaseUrl)/api/admin/queue/stop"
        ContentType = "application/x-www-form-urlencoded"
        Body = ""
    }
    $null = Invoke-RestMethod @request
    Wait-ForCondition {
        $current = Get-ServiceStatus
        $null -ne $current -and
            -not [bool]$current.workerQueueRunning -and
            -not [bool]$current.workerQueueStopping
    } "Worker queue did not stop within $TimeoutSeconds seconds"
    Write-ControlLog "INFO" "Worker queue stopped cleanly."
}

function Stop-RunningService {
    param([object]$Status)

    $listenerProcessId = Get-ListenerProcessId
    if (-not $listenerProcessId) {
        Write-ControlLog "INFO" "No service listener is running on port $($serviceConfig.Port)."
        return [pscustomobject]@{ ServiceProcessId = 0; LauncherProcessId = 0 }
    }

    $serviceProcessId = if ($null -ne $Status) { [int]$Status.service.pid } else { $listenerProcessId }
    if ($serviceProcessId -ne $listenerProcessId) {
        throw "Status PID $serviceProcessId does not own port $($serviceConfig.Port); listener PID is $listenerProcessId"
    }

    $serviceProcess = Get-ProcessRecord $serviceProcessId
    if (-not (Test-YtLibraryProcess $serviceProcess)) {
        throw "Refusing to stop PID $serviceProcessId because it is not a YT Library service"
    }

    $launcher = Get-VenvLauncherRecord $serviceProcessId
    if ($null -eq $launcher -and -not $Force) {
        throw "Service PID $serviceProcessId was not launched through $venvPython; use -Force to replace it"
    }
    $launcherProcessId = if ($null -ne $launcher) { [int]$launcher.ProcessId } else { 0 }

    Write-ControlLog "INFO" "Stopping service PID $serviceProcessId; venv launcher PID $launcherProcessId."
    Stop-Process -Id $serviceProcessId
    Wait-ForCondition {
        -not (Get-ProcessRecord $serviceProcessId)
    } "Service PID $serviceProcessId did not stop within $TimeoutSeconds seconds"

    if ($launcherProcessId -and $launcherProcessId -ne $serviceProcessId) {
        $remainingLauncher = Get-ProcessRecord $launcherProcessId
        if ($null -ne $remainingLauncher) {
            Stop-Process -Id $launcherProcessId
            Wait-ForCondition {
                -not (Get-ProcessRecord $launcherProcessId)
            } "Venv launcher PID $launcherProcessId did not stop within $TimeoutSeconds seconds"
        }
    }

    Wait-ForCondition {
        -not (Get-ListenerProcessId)
    } "Port $($serviceConfig.Port) remained open after stopping the service"
    Write-ControlLog "INFO" "Service stopped; port $($serviceConfig.Port) is closed."

    return [pscustomobject]@{
        ServiceProcessId = $serviceProcessId
        LauncherProcessId = $launcherProcessId
    }
}

function Start-ServiceProcess {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Project venv Python was not found: $venvPython"
    }
    if (Get-ListenerProcessId) {
        throw "Port $($serviceConfig.Port) is already in use"
    }

    Reset-ServiceRunLogs
    $stdoutPath = Join-Path $logDirectory "service.stdout.log"
    $stderrPath = Join-Path $logDirectory "service.stderr.log"
    $start = @{
        FilePath = $venvPython
        ArgumentList = $managerScript
        WorkingDirectory = $repoRoot
        WindowStyle = "Hidden"
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError = $stderrPath
        PassThru = $true
    }
    $launcher = Start-Process @start
    Write-ControlLog "INFO" "Started venv launcher PID $($launcher.Id); stdout=$stdoutPath; stderr=$stderrPath."

    try {
        Wait-ForCondition {
            $launcher.Refresh()
            if ($launcher.HasExited) {
                throw "Venv launcher exited with code $($launcher.ExitCode); see $stderrPath"
            }
            $currentStatus = Get-ServiceStatus
            $null -ne $currentStatus -and $currentStatus.service.status -eq "running"
        } "Service did not become healthy within $TimeoutSeconds seconds; see $stderrPath"

        $healthyStatus = Get-ServiceStatus
        if ($null -eq $healthyStatus) {
            throw "Service health was lost after startup; see $stderrPath"
        }
        $serviceProcessId = [int]$healthyStatus.service.pid
        $listenerProcessId = Get-ListenerProcessId
        if ($listenerProcessId -ne $serviceProcessId) {
            throw "Healthy service PID $serviceProcessId does not own port $($serviceConfig.Port)"
        }
        $venvLauncher = Get-VenvLauncherRecord $serviceProcessId
        if ($null -eq $venvLauncher -or [int]$venvLauncher.ProcessId -ne $launcher.Id) {
            throw "Service PID $serviceProcessId is not a child of venv launcher PID $($launcher.Id)"
        }

        Write-ControlLog "INFO" "Service healthy at $($serviceConfig.BaseUrl); PID $serviceProcessId; venv launcher PID $($launcher.Id)."
        return [pscustomobject]@{
            Status = $healthyStatus
            ServiceProcessId = $serviceProcessId
            LauncherProcessId = [int]$venvLauncher.ProcessId
            Stdout = $stdoutPath
            Stderr = $stderrPath
        }
    }
    catch {
        Write-ControlLog "ERROR" "Service launch verification failed: $($_.Exception.Message)"
        $listenerProcessId = Get-ListenerProcessId
        if ($listenerProcessId) {
            $listener = Get-ProcessRecord $listenerProcessId
            if (Test-YtLibraryProcess $listener) {
                Stop-Process -Id $listenerProcessId -ErrorAction SilentlyContinue
            }
        }
        if (Get-ProcessRecord $launcher.Id) {
            Stop-Process -Id $launcher.Id -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Resume-Queue {
    Write-ControlLog "INFO" "Requesting worker queue resume."
    $request = @{
        Method = "Post"
        Uri = "$($serviceConfig.BaseUrl)/api/admin/queue/start"
        ContentType = "application/x-www-form-urlencoded"
        Body = ""
    }
    $response = Invoke-RestMethod @request
    $blocked = [bool](Get-ObjectValue $response.dispatcher "blocked" $false)
    if ($blocked) {
        throw "Service restarted, but queue resume was blocked: $($response.dispatcher.message)"
    }
    Write-ControlLog "INFO" "Worker queue resume accepted: $($response.dispatcher.message)"
    return $response.dispatcher
}

function Write-Result {
    param([object]$Result)

    $Result | Add-Member -NotePropertyName OperationLockWaited -NotePropertyValue $operationLockWaited
    $Result | Add-Member -NotePropertyName OperationLockWaitSeconds -NotePropertyValue $operationLockWaitSeconds
    if ($Json) {
        Write-Output ($Result | ConvertTo-Json -Depth 6 -Compress)
    }
    else {
        Write-Output $Result
    }
}

$operationMutex = $null
$operationLockHeld = $false

try {
    if ($Action -ne "status") {
        $operationMutex = [System.Threading.Mutex]::new($false, $operationMutexName)
        try {
            $operationLockHeld = $operationMutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            $operationLockHeld = $true
            Write-ControlLog "WARN" "Recovered abandoned service-operation lock $operationMutexName."
        }
        if (-not $operationLockHeld) {
            $operationLockWaited = $true
            $waitMessage = "Another service operation is active; waiting up to $TimeoutSeconds seconds for the service-operation lock."
            Write-Warning $waitMessage
            $lockWait = [System.Diagnostics.Stopwatch]::StartNew()
            try {
                $operationLockHeld = $operationMutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
            }
            catch [System.Threading.AbandonedMutexException] {
                $operationLockHeld = $true
                Write-ControlLog "WARN" "Recovered abandoned service-operation lock $operationMutexName while waiting."
            }
            finally {
                $lockWait.Stop()
                $operationLockWaitSeconds = [Math]::Round($lockWait.Elapsed.TotalSeconds, 3)
            }
            if ($operationLockHeld) {
                Write-ControlLog "INFO" "Acquired service-operation lock after waiting $operationLockWaitSeconds seconds."
            }
        }
        if (-not $operationLockHeld) {
            throw "Another service operation is still running after waiting $TimeoutSeconds seconds"
        }
    }

    $initialStatus = Get-ServiceStatus
    $initialListenerProcessId = Get-ListenerProcessId
    $queueWasRunning = $null -ne $initialStatus -and [bool]$initialStatus.workerQueueRunning
    $queueCount = if ($null -ne $initialStatus) { [int]$initialStatus.workerQueueCount } else { 0 }
    if ($Action -ne "status") {
        Write-ControlLog "INFO" "Action=$Action; url=$($serviceConfig.BaseUrl); listener_pid=$initialListenerProcessId; queue_running=$queueWasRunning; queue_count=$queueCount."
    }

    switch ($Action) {
        "status" {
            $state = if ($null -ne $initialStatus) { "running" } elseif ($initialListenerProcessId) { "unhealthy" } else { "stopped" }
            $launcher = if ($initialListenerProcessId) { Get-VenvLauncherRecord $initialListenerProcessId } else { $null }
            Write-Result ([pscustomobject]@{
                Action = "status"
                State = $state
                Url = $serviceConfig.BaseUrl
                ServicePid = $initialListenerProcessId
                LauncherPid = if ($null -ne $launcher) { [int]$launcher.ProcessId } else { 0 }
                QueueRunning = $queueWasRunning
                QueueCount = $queueCount
                ProxyBlocked = $null -ne $initialStatus -and [bool]$initialStatus.proxyBlock.blocked
                ControlLog = $controlLogPath
            })
        }
        "start" {
            if ($null -ne $initialStatus) {
                Write-ControlLog "INFO" "Start skipped because service PID $([int]$initialStatus.service.pid) is already healthy."
                Write-Result ([pscustomobject]@{
                    Action = "start"
                    State = "already running"
                    Url = $serviceConfig.BaseUrl
                    ServicePid = [int]$initialStatus.service.pid
                    QueueRunning = [bool]$initialStatus.workerQueueRunning
                    QueueCount = [int]$initialStatus.workerQueueCount
                    ControlLog = $controlLogPath
                })
                break
            }
            if ($initialListenerProcessId) {
                throw "Port $($serviceConfig.Port) is occupied by an unhealthy process; use restart -Force after inspecting it"
            }
            $started = Start-ServiceProcess
            Write-ControlLog "INFO" "Start complete; service PID $($started.ServiceProcessId)."
            Write-Result ([pscustomobject]@{
                Action = "start"
                State = "running"
                Url = $serviceConfig.BaseUrl
                ServicePid = $started.ServiceProcessId
                LauncherPid = $started.LauncherProcessId
                QueueRunning = [bool]$started.Status.workerQueueRunning
                QueueCount = [int]$started.Status.workerQueueCount
                Stdout = $started.Stdout
                Stderr = $started.Stderr
                ControlLog = $controlLogPath
            })
        }
        "stop" {
            if ($null -eq $initialStatus -and $initialListenerProcessId -and -not $Force) {
                throw "The listener is not responding to status; use stop -Force after inspecting it"
            }
            Stop-QueueIfRunning $initialStatus
            $stopped = Stop-RunningService $initialStatus
            Write-ControlLog "INFO" "Stop complete; previous service PID $($stopped.ServiceProcessId)."
            Write-Result ([pscustomobject]@{
                Action = "stop"
                State = "stopped"
                Url = $serviceConfig.BaseUrl
                PreviousServicePid = $stopped.ServiceProcessId
                PreviousLauncherPid = $stopped.LauncherProcessId
                QueueWasRunning = $queueWasRunning
                QueueCount = $queueCount
                ControlLog = $controlLogPath
            })
        }
        "restart" {
            if ($null -eq $initialStatus -and $initialListenerProcessId -and -not $Force) {
                throw "The listener is not responding to status; use restart -Force after inspecting it"
            }
            Stop-QueueIfRunning $initialStatus
            $stopped = Stop-RunningService $initialStatus
            $started = Start-ServiceProcess
            $resumeResult = $null
            if ($queueWasRunning) {
                $resumeResult = Resume-Queue
            }
            $finalStatus = Get-ServiceStatus
            if ($null -eq $finalStatus) {
                throw "Service health was lost after restart"
            }
            Write-ControlLog "INFO" "Restart complete; old PID $($stopped.ServiceProcessId); new PID $($started.ServiceProcessId); queue_resumed=$($queueWasRunning -and $null -ne $resumeResult)."
            Write-Result ([pscustomobject]@{
                Action = "restart"
                State = "running"
                Url = $serviceConfig.BaseUrl
                PreviousServicePid = $stopped.ServiceProcessId
                ServicePid = $started.ServiceProcessId
                LauncherPid = $started.LauncherProcessId
                QueueWasRunning = $queueWasRunning
                QueueResumed = $queueWasRunning -and $null -ne $resumeResult
                QueueRunning = $null -ne $finalStatus -and [bool]$finalStatus.workerQueueRunning
                QueueCount = if ($null -ne $finalStatus) { [int]$finalStatus.workerQueueCount } else { $queueCount }
                ProxyBlocked = $null -ne $finalStatus -and [bool]$finalStatus.proxyBlock.blocked
                Stdout = $started.Stdout
                Stderr = $started.Stderr
                ControlLog = $controlLogPath
            })
        }
    }
}
catch {
    if ($operationLockHeld) {
        Write-ControlLog "ERROR" "Action=$Action failed: $($_.Exception.Message)"
    }
    throw
}
finally {
    if ($operationLockHeld -and $null -ne $operationMutex) {
        $operationMutex.ReleaseMutex()
    }
    if ($null -ne $operationMutex) {
        $operationMutex.Dispose()
    }
}
