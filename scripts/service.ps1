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
and reports the lock wait plus any known operation ID, action, and stage in the
returned result. Restart intent is persisted before shutdown so a later start
can finish an interrupted restart and restore a previously running queue.

Mutating operations write transitions and failures to the bounded rolling
.codex\service-logs\service-control.log. The current service run writes to the
fixed service.stdout.log and service.stderr.log paths in that directory. New
launches replace the stream logs and remove legacy service-launch files.

.PARAMETER Action
status, start, restart, or stop. The default is status.

.PARAMETER TimeoutSeconds
Maximum time to wait for each queue, process, port, health, or controller-lock
transition. The default accommodates this installation's slower startup.

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
    [int]$TimeoutSeconds = 90,

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
$recoveryStatePath = Join-Path $logDirectory "service-recovery.json"
$controlLogMaxBytes = 262144
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$repoNameBytes = [System.Text.Encoding]::UTF8.GetBytes($repoRoot.ToLowerInvariant())
$repoNameHash = [System.Security.Cryptography.SHA256]::HashData($repoNameBytes)
$repoNameToken = [System.Convert]::ToHexString($repoNameHash).Substring(0, 16)
$operationMutexName = "Local\YTLibraryServiceControl-$repoNameToken"
$operationLockWaited = $false
$operationLockWaitSeconds = 0.0
$contendedOperationId = ""
$contendedOperationAction = ""
$contendedOperationStage = ""
$contendedControllerPid = 0

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

function Write-RecoveryState {
    param([object]$State)

    $temporaryPath = "$recoveryStatePath.$PID.tmp"
    $json = $State | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        $json,
        [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::Move($temporaryPath, $recoveryStatePath, $true)
}

function Get-RecoveryState {
    if (-not (Test-Path -LiteralPath $recoveryStatePath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $recoveryStatePath -Raw | ConvertFrom-Json
    }
    catch {
        Write-ControlLog "WARN" "Could not read restart recovery state: $($_.Exception.Message)"
        return $null
    }
}

function New-RestartRecoveryState {
    param(
        [int]$OldPid,
        [bool]$QueueWasRunning,
        [int]$QueueCount
    )

    $now = (Get-Date).ToUniversalTime().ToString("o")
    $state = [pscustomobject][ordered]@{
        operationId = [guid]::NewGuid().ToString()
        action = "restart"
        controllerPid = $PID
        oldPid = $OldPid
        queueWasRunning = $QueueWasRunning
        queueCount = $QueueCount
        stage = "preparing"
        startedAt = $now
        updatedAt = $now
        launcherPid = 0
        servicePid = 0
        lastError = ""
    }
    Write-RecoveryState $state
    Write-ControlLog "INFO" "Persisted restart recovery operation $($state.operationId); queue_running=$QueueWasRunning; queue_count=$QueueCount."
    return $state
}

function Update-RecoveryState {
    param(
        [object]$State,
        [string]$Stage,
        [hashtable]$Values = @{}
    )

    if ($null -eq $State) {
        return
    }
    $State | Add-Member -NotePropertyName stage -NotePropertyValue $Stage -Force
    $State | Add-Member -NotePropertyName updatedAt -NotePropertyValue ((Get-Date).ToUniversalTime().ToString("o")) -Force
    foreach ($entry in $Values.GetEnumerator()) {
        $State | Add-Member -NotePropertyName $entry.Key -NotePropertyValue $entry.Value -Force
    }
    Write-RecoveryState $State
    Write-ControlLog "INFO" "Restart recovery operation $($State.operationId) stage=$Stage."
}

function Clear-RecoveryState {
    param([object]$State)

    if (Test-Path -LiteralPath $recoveryStatePath -PathType Leaf) {
        Remove-Item -LiteralPath $recoveryStatePath -Force
    }
    if ($null -ne $State) {
        Write-ControlLog "INFO" "Cleared restart recovery operation $($State.operationId)."
    }
}

function Get-ServiceConfiguration {
    $config = [pscustomobject]@{}
    if (Test-Path -LiteralPath $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    }

    $bindHost = [string](Get-ObjectValue $config "host" "127.0.0.1")
    $port = [int](Get-ObjectValue $config "port" 8765)
    $databaseSetting = [string](Get-ObjectValue $config "database" "yt_library.sqlite3")
    $databasePath = if ([System.IO.Path]::IsPathRooted($databaseSetting)) {
        [System.IO.Path]::GetFullPath($databaseSetting)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $repoRoot $databaseSetting))
    }
    $probeHost = if ($bindHost -in @("0.0.0.0", "::")) { "127.0.0.1" } else { $bindHost }
    $uriHost = if ($probeHost.Contains(":")) { "[$probeHost]" } else { $probeHost }

    return [pscustomobject]@{
        BindHost = $bindHost
        Port = $port
        BaseUrl = "http://${uriHost}:$port"
        DatabasePath = $databasePath
    }
}

$serviceConfig = Get-ServiceConfiguration

function Get-PersistentQueueCount {
    if (
        -not (Test-Path -LiteralPath $venvPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath $serviceConfig.DatabasePath -PathType Leaf)
    ) {
        return 0
    }

    $query = @'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5)
try:
    print(connection.execute("SELECT COUNT(*) FROM worker_queue").fetchone()[0])
finally:
    connection.close()
'@
    try {
        $value = & $venvPython -c $query $serviceConfig.DatabasePath 2>$null
        if ($LASTEXITCODE -eq 0 -and $null -ne $value) {
            return [int]$value
        }
    }
    catch {
        Write-Verbose "Persistent queue count unavailable: $($_.Exception.Message)"
    }
    return 0
}

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

function Wait-ForServiceStartup {
    param(
        [System.Diagnostics.Process]$Launcher,
        [string]$StderrPath
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $Launcher.Refresh()
        if ($Launcher.HasExited) {
            throw "Venv launcher exited with code $($Launcher.ExitCode); see $StderrPath"
        }
        $currentStatus = Get-ServiceStatus
        if ($null -ne $currentStatus -and $currentStatus.service.status -eq "running") {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    $Launcher.Refresh()
    if ($Launcher.HasExited) {
        throw "Venv launcher exited with code $($Launcher.ExitCode); see $StderrPath"
    }
    throw "Venv launcher PID $($Launcher.Id) remains alive, but the service did not become healthy within $TimeoutSeconds seconds; see $StderrPath"
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
    param([object]$RecoveryState = $null)

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
    Update-RecoveryState $RecoveryState "launching" @{
        launcherPid = [int]$launcher.Id
        lastError = ""
    }

    try {
        Wait-ForServiceStartup $launcher $stderrPath

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
        $recoveryStage = if (
            $null -ne $RecoveryState -and
            [bool](Get-ObjectValue $RecoveryState "queueWasRunning" $false)
        ) { "queue_resume_pending" } else { "verifying" }
        Update-RecoveryState $RecoveryState $recoveryStage @{
            servicePid = $serviceProcessId
            launcherPid = [int]$launcher.Id
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

function Complete-Recovery {
    param(
        [object]$RecoveryState,
        [object]$CurrentStatus
    )

    $queueShouldRun = (
        $null -ne $RecoveryState -and
        [bool](Get-ObjectValue $RecoveryState "queueWasRunning" $false)
    )
    $resumeResult = $null
    if ($queueShouldRun -and ($null -eq $CurrentStatus -or -not [bool]$CurrentStatus.workerQueueRunning)) {
        Update-RecoveryState $RecoveryState "queue_resume_pending"
        $resumeResult = Resume-Queue
        Wait-ForCondition {
            $status = Get-ServiceStatus
            $null -ne $status -and [bool]$status.workerQueueRunning
        } "Worker queue did not resume within $TimeoutSeconds seconds"
    }
    elseif ($queueShouldRun) {
        Write-ControlLog "INFO" "Worker queue is already running; no recovery resume is needed."
    }

    $finalStatus = Get-ServiceStatus
    if ($null -eq $finalStatus) {
        throw "Service health was lost while completing restart recovery"
    }
    if ($queueShouldRun -and -not [bool]$finalStatus.workerQueueRunning) {
        throw "Service is healthy, but the worker queue recovery is still pending"
    }
    Clear-RecoveryState $RecoveryState
    return [pscustomobject]@{
        Status = $finalStatus
        QueueResumed = $queueShouldRun -and $null -ne $resumeResult
    }
}

function Write-Result {
    param([object]$Result)

    $Result | Add-Member -NotePropertyName OperationLockWaited -NotePropertyValue $operationLockWaited
    $Result | Add-Member -NotePropertyName OperationLockWaitSeconds -NotePropertyValue $operationLockWaitSeconds
    $Result | Add-Member -NotePropertyName ContendedOperationId -NotePropertyValue $contendedOperationId
    $Result | Add-Member -NotePropertyName ContendedOperationAction -NotePropertyValue $contendedOperationAction
    $Result | Add-Member -NotePropertyName ContendedOperationStage -NotePropertyValue $contendedOperationStage
    $Result | Add-Member -NotePropertyName ContendedControllerPid -NotePropertyValue $contendedControllerPid
    if ($Json) {
        Write-Output ($Result | ConvertTo-Json -Depth 6 -Compress)
    }
    else {
        Write-Output $Result
    }
}

$operationMutex = $null
$operationLockHeld = $false
$activeRecoveryState = $null

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
            $contendedState = Get-RecoveryState
            if ($null -ne $contendedState) {
                $contendedOperationId = [string](Get-ObjectValue $contendedState "operationId" "")
                $contendedOperationAction = [string](Get-ObjectValue $contendedState "action" "")
                $contendedOperationStage = [string](Get-ObjectValue $contendedState "stage" "")
                $contendedControllerPid = [int](Get-ObjectValue $contendedState "controllerPid" 0)
            }
            $operationDescription = if ($contendedOperationId) {
                "$contendedOperationAction operation $contendedOperationId (controller PID $contendedControllerPid) at stage $contendedOperationStage"
            }
            else {
                "another service operation"
            }
            $waitMessage = "Service-operation contention: $operationDescription is active; waiting up to $TimeoutSeconds seconds for the lock."
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
                Write-ControlLog "INFO" "Acquired service-operation lock after waiting $operationLockWaitSeconds seconds; contended_operation=$contendedOperationId; contended_action=$contendedOperationAction; contended_stage=$contendedOperationStage; contended_controller_pid=$contendedControllerPid."
            }
        }
        if (-not $operationLockHeld) {
            throw "Service-operation contention did not clear within $TimeoutSeconds seconds; operation=$contendedOperationId; action=$contendedOperationAction; stage=$contendedOperationStage; controller_pid=$contendedControllerPid"
        }
    }

    $initialStatus = Get-ServiceStatus
    $initialListenerProcessId = Get-ListenerProcessId
    $recoveryState = Get-RecoveryState
    $queueIsRunning = $null -ne $initialStatus -and [bool]$initialStatus.workerQueueRunning
    $recoveryExpectedQueue = (
        $null -ne $recoveryState -and
        [bool](Get-ObjectValue $recoveryState "queueWasRunning" $false)
    )
    $queueWasRunning = $queueIsRunning -or $recoveryExpectedQueue
    $queueCount = if ($null -ne $initialStatus) {
        [int]$initialStatus.workerQueueCount
    }
    else {
        Get-PersistentQueueCount
    }
    if ($Action -ne "status") {
        $recoveryOperationId = if ($null -ne $recoveryState) { [string]$recoveryState.operationId } else { "" }
        Write-ControlLog "INFO" "Action=$Action; url=$($serviceConfig.BaseUrl); listener_pid=$initialListenerProcessId; queue_running=$queueIsRunning; queue_should_run=$queueWasRunning; queue_count=$queueCount; recovery_operation=$recoveryOperationId."
    }

    switch ($Action) {
        "status" {
            $state = if ($null -ne $recoveryState) {
                switch ([string](Get-ObjectValue $recoveryState "stage" "recovering")) {
                    { $_ -in @("preparing", "stopping_queue", "stopping_service", "launching") } { "restarting"; break }
                    default { "recovering" }
                }
            }
            elseif ($null -ne $initialStatus) { "running" }
            elseif ($initialListenerProcessId) { "unhealthy" }
            else { "stopped" }
            $launcher = if ($initialListenerProcessId) { Get-VenvLauncherRecord $initialListenerProcessId } else { $null }
            $recoveryLauncherPid = if ($null -ne $recoveryState) {
                [int](Get-ObjectValue $recoveryState "launcherPid" 0)
            }
            else { 0 }
            Write-Result ([pscustomobject]@{
                Action = "status"
                State = $state
                Url = $serviceConfig.BaseUrl
                ServicePid = $initialListenerProcessId
                LauncherPid = if ($null -ne $launcher) { [int]$launcher.ProcessId } else { $recoveryLauncherPid }
                QueueRunning = $queueIsRunning
                QueueCount = $queueCount
                QueueResumePending = $recoveryExpectedQueue -and -not $queueIsRunning
                OperationId = if ($null -ne $recoveryState) { [string]$recoveryState.operationId } else { "" }
                OperationStage = if ($null -ne $recoveryState) { [string]$recoveryState.stage } else { "" }
                ControllerPid = if ($null -ne $recoveryState) { [int](Get-ObjectValue $recoveryState "controllerPid" 0) } else { 0 }
                ProxyBlocked = $null -ne $initialStatus -and [bool]$initialStatus.proxyBlock.blocked
                ControlLog = $controlLogPath
                RecoveryState = $recoveryStatePath
            })
        }
        "start" {
            $activeRecoveryState = $recoveryState
            if ($null -ne $initialStatus) {
                $completed = if ($null -ne $recoveryState) {
                    Write-ControlLog "INFO" "Healthy service found with pending restart recovery; completing queue restoration."
                    Complete-Recovery $recoveryState $initialStatus
                }
                else {
                    [pscustomobject]@{ Status = $initialStatus; QueueResumed = $false }
                }
                $activeRecoveryState = $null
                $finalStatus = $completed.Status
                Write-ControlLog "INFO" "Start skipped because service PID $([int]$finalStatus.service.pid) is already healthy."
                Write-Result ([pscustomobject]@{
                    Action = "start"
                    State = if ($null -ne $recoveryState) { "recovered" } else { "already running" }
                    Url = $serviceConfig.BaseUrl
                    ServicePid = [int]$finalStatus.service.pid
                    QueueRecovered = [bool]$completed.QueueResumed
                    QueueRunning = [bool]$finalStatus.workerQueueRunning
                    QueueCount = [int]$finalStatus.workerQueueCount
                    ControlLog = $controlLogPath
                })
                break
            }
            if ($initialListenerProcessId) {
                throw "Port $($serviceConfig.Port) is occupied by an unhealthy process; use restart -Force after inspecting it"
            }
            $started = Start-ServiceProcess $recoveryState
            $completed = if ($null -ne $recoveryState) {
                Complete-Recovery $recoveryState $started.Status
            }
            else {
                [pscustomobject]@{ Status = $started.Status; QueueResumed = $false }
            }
            $activeRecoveryState = $null
            $finalStatus = $completed.Status
            Write-ControlLog "INFO" "Start complete; service PID $($started.ServiceProcessId)."
            Write-Result ([pscustomobject]@{
                Action = "start"
                State = "running"
                Url = $serviceConfig.BaseUrl
                ServicePid = $started.ServiceProcessId
                LauncherPid = $started.LauncherProcessId
                QueueRecovered = [bool]$completed.QueueResumed
                QueueRunning = [bool]$finalStatus.workerQueueRunning
                QueueCount = [int]$finalStatus.workerQueueCount
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
            Clear-RecoveryState $recoveryState
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
            $activeRecoveryState = New-RestartRecoveryState $initialListenerProcessId $queueWasRunning $queueCount
            Update-RecoveryState $activeRecoveryState "stopping_queue"
            Stop-QueueIfRunning $initialStatus
            Update-RecoveryState $activeRecoveryState "stopping_service"
            $stopped = Stop-RunningService $initialStatus
            $started = Start-ServiceProcess $activeRecoveryState
            $completed = Complete-Recovery $activeRecoveryState $started.Status
            $activeRecoveryState = $null
            $finalStatus = $completed.Status
            Write-ControlLog "INFO" "Restart complete; old PID $($stopped.ServiceProcessId); new PID $($started.ServiceProcessId); queue_resumed=$($completed.QueueResumed)."
            Write-Result ([pscustomobject]@{
                Action = "restart"
                State = "running"
                Url = $serviceConfig.BaseUrl
                PreviousServicePid = $stopped.ServiceProcessId
                ServicePid = $started.ServiceProcessId
                LauncherPid = $started.LauncherProcessId
                QueueWasRunning = $queueWasRunning
                QueueResumed = [bool]$completed.QueueResumed
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
    $actionError = $_.Exception.Message
    if ($operationLockHeld) {
        if ($null -ne $activeRecoveryState) {
            try {
                Update-RecoveryState $activeRecoveryState "failed" @{
                    lastError = $actionError
                }
            }
            catch {
                Write-ControlLog "ERROR" "Could not persist restart recovery failure: $($_.Exception.Message)"
            }
        }
        Write-ControlLog "ERROR" "Action=$Action failed: $actionError"
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
