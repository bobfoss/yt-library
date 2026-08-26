<#
.SYNOPSIS
Installs or removes the per-repository YT Library Windows service.

.DESCRIPTION
Run this script from an elevated PowerShell window. Installation prompts for
the current Windows account password, grants that account Log on as a service,
registers an automatic delayed-start service, configures crash recovery, and
then delegates startup and verification to scripts\service.ps1. The password is
passed only as a PSCredential and is never written to a file or command line.

Normal status, start, restart, and stop operations continue to use
scripts\service.ps1 and do not require elevation after installation.

.PARAMETER Action
install, update-credential, uninstall, or status. The default is status.

.EXAMPLE
.\scripts\windows-service.ps1 install

.EXAMPLE
.\scripts\windows-service.ps1 update-credential

.EXAMPLE
.\scripts\windows-service.ps1 uninstall
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "update-credential", "uninstall", "status")]
    [string]$Action = "status",

    [ValidateRange(15, 300)]
    [int]$TimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$controllerScript = Join-Path $PSScriptRoot "service.ps1"
$runtimeScript = Join-Path $PSScriptRoot "service_runtime.py"
$hostScript = Join-Path $PSScriptRoot "windows_service_host.py"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$logDirectory = Join-Path $repoRoot ".codex\service-logs"
$repoNameBytes = [System.Text.Encoding]::UTF8.GetBytes($repoRoot.ToLowerInvariant())
$repoNameHash = [System.Security.Cryptography.SHA256]::HashData($repoNameBytes)
$repoNameToken = [System.Convert]::ToHexString($repoNameHash).Substring(0, 16)
$serviceName = "YTLibraryManager-$repoNameToken"
$displayName = "YT Library Manager"
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentAccountName = $currentIdentity.Name
$currentAccountSid = $currentIdentity.User.Value

function Assert-Elevated {
    $principal = [System.Security.Principal.WindowsPrincipal]::new(
        [System.Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Action '$Action' requires an elevated PowerShell window. Open PowerShell with Run as administrator, return to '$repoRoot', and run this command again."
    }
}

function Get-WindowsServiceRecord {
    return Get-CimInstance -ClassName Win32_Service -Filter "Name='$serviceName'" -ErrorAction SilentlyContinue
}

function Invoke-Controller {
    param([string]$ControllerAction)

    $raw = & $controllerScript $ControllerAction -TimeoutSeconds $TimeoutSeconds -Json
    return ($raw | Select-Object -Last 1 | ConvertFrom-Json)
}

function Set-QueueIntent {
    param([bool]$ShouldRun, [string]$Source)

    $value = if ($ShouldRun) { "running" } else { "stopped" }
    $null = & $venvPython $runtimeScript --log-directory $logDirectory queue-intent $value --source $Source
    if ($LASTEXITCODE -ne 0) {
        throw "Could not persist worker queue intent"
    }
}

function Get-ServiceCredential {
    $credential = Get-Credential -UserName $currentAccountName -Message (
        "Enter the Windows account password for $currentAccountName. " +
        "A Windows Hello PIN will not work."
    )
    if ($null -eq $credential) {
        throw "Credential entry was cancelled"
    }
    $credentialSid = ([System.Security.Principal.NTAccount]::new(
        $credential.UserName
    )).Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($credentialSid -ne $currentAccountSid) {
        throw "This service must use the current account $currentAccountName so it can access this user-owned repository and runtime data"
    }
    return $credential
}

function Add-LogOnAsServiceRight {
    if (-not ("YtLibrary.ServiceAccountRights" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

namespace YtLibrary {
    public static class ServiceAccountRights {
        [StructLayout(LayoutKind.Sequential)]
        private struct LsaObjectAttributes {
            public int Length;
            public IntPtr RootDirectory;
            public IntPtr ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct LsaUnicodeString {
            public ushort Length;
            public ushort MaximumLength;
            public IntPtr Buffer;
        }

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint LsaOpenPolicy(
            IntPtr systemName,
            ref LsaObjectAttributes objectAttributes,
            uint desiredAccess,
            out IntPtr policyHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint LsaAddAccountRights(
            IntPtr policyHandle,
            IntPtr accountSid,
            LsaUnicodeString[] userRights,
            uint countOfRights);

        [DllImport("advapi32.dll")]
        private static extern uint LsaNtStatusToWinError(uint status);

        [DllImport("advapi32.dll")]
        private static extern uint LsaClose(IntPtr policyHandle);

        public static void GrantLogOnAsService(string sidValue) {
            const uint PolicyCreateAccount = 0x00000010;
            const uint PolicyLookupNames = 0x00000800;
            var attributes = new LsaObjectAttributes();
            attributes.Length = Marshal.SizeOf(attributes);
            IntPtr policyHandle;
            uint status = LsaOpenPolicy(
                IntPtr.Zero,
                ref attributes,
                PolicyCreateAccount | PolicyLookupNames,
                out policyHandle);
            ThrowIfFailed(status, "LsaOpenPolicy");

            IntPtr sidPointer = IntPtr.Zero;
            IntPtr rightPointer = IntPtr.Zero;
            try {
                var sid = new SecurityIdentifier(sidValue);
                byte[] sidBytes = new byte[sid.BinaryLength];
                sid.GetBinaryForm(sidBytes, 0);
                sidPointer = Marshal.AllocHGlobal(sidBytes.Length);
                Marshal.Copy(sidBytes, 0, sidPointer, sidBytes.Length);

                const string rightName = "SeServiceLogonRight";
                rightPointer = Marshal.StringToHGlobalUni(rightName);
                var rights = new[] {
                    new LsaUnicodeString {
                        Buffer = rightPointer,
                        Length = checked((ushort)(rightName.Length * 2)),
                        MaximumLength = checked((ushort)((rightName.Length + 1) * 2))
                    }
                };
                status = LsaAddAccountRights(policyHandle, sidPointer, rights, 1);
                ThrowIfFailed(status, "LsaAddAccountRights");
            }
            finally {
                if (rightPointer != IntPtr.Zero) Marshal.FreeHGlobal(rightPointer);
                if (sidPointer != IntPtr.Zero) Marshal.FreeHGlobal(sidPointer);
                LsaClose(policyHandle);
            }
        }

        private static void ThrowIfFailed(uint status, string operation) {
            if (status == 0) return;
            int error = unchecked((int)LsaNtStatusToWinError(status));
            throw new Win32Exception(error, operation + " failed");
        }
    }
}
'@
    }
    [YtLibrary.ServiceAccountRights]::GrantLogOnAsService($currentAccountSid)
}

function Get-BasePython {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Project venv Python was not found: $venvPython"
    }
    $basePython = (& $venvPython -c "import sys; print(sys._base_executable or sys.executable)" | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
        throw "Could not resolve the base Python executable from $venvPython"
    }
    return [System.IO.Path]::GetFullPath($basePython)
}

function Get-ServiceSecurityDescriptor {
    $controllerAccess = "(A;;CCLCSWRPWPLO;;;$currentAccountSid)"
    return (
        "D:" +
        "(A;;CCLCSWRPWPDTLOCRRC;;;SY)" +
        "(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)" +
        "(A;;CCLCSWLOCRRC;;;SU)" +
        $controllerAccess
    )
}

function Configure-ServiceRecovery {
    $sc = Join-Path $env:SystemRoot "System32\sc.exe"
    $null = & $sc failure $serviceName "reset=" "86400" "actions=" "restart/5000/restart/15000/restart/60000"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure SCM recovery actions for $serviceName"
    }
    $null = & $sc failureflag $serviceName "1"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not enable SCM recovery for non-crash service failures"
    }
}

function Wait-ForServiceRemoval {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        if ($null -eq (Get-WindowsServiceRecord)) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "Windows service $serviceName is still pending deletion"
}

if ($Action -eq "status") {
    $service = Get-WindowsServiceRecord
    $controller = Invoke-Controller "status"
    [pscustomobject]@{
        Action = "status"
        Installed = $null -ne $service
        ServiceName = $serviceName
        DisplayName = if ($null -ne $service) { [string]$service.DisplayName } else { $displayName }
        State = if ($null -ne $service) { [string]$service.State } else { "not installed" }
        StartMode = if ($null -ne $service) { [string]$service.StartMode } else { "" }
        Account = if ($null -ne $service) { [string]$service.StartName } else { "" }
        ProcessId = if ($null -ne $service) { [int]$service.ProcessId } else { 0 }
        Controller = $controller
    }
    exit 0
}

Assert-Elevated

if ($Action -eq "install") {
    if ($null -ne (Get-WindowsServiceRecord)) {
        throw "Windows service $serviceName is already installed. Use update-credential to refresh its password or uninstall to remove it."
    }
    foreach ($requiredPath in @($controllerScript, $runtimeScript, $hostScript, $venvPython)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Required service file was not found: $requiredPath"
        }
    }

    $credential = Get-ServiceCredential
    Add-LogOnAsServiceRight
    $basePython = Get-BasePython
    $binaryPath = "`"$basePython`" -B `"$hostScript`" --repo-root `"$repoRoot`" --service-name `"$serviceName`""
    $previous = Invoke-Controller "status"
    $queueShouldRun = [bool]$previous.QueueRunning -or [bool]$previous.QueueResumePending
    $directWasRunning = [string]$previous.State -ne "stopped"
    $created = $false
    try {
        if ($directWasRunning) {
            $null = Invoke-Controller "stop"
        }
        Set-QueueIntent $queueShouldRun "windows-service-install"
        $null = New-Service -Name $serviceName `
            -BinaryPathName $binaryPath `
            -DisplayName $displayName `
            -Description "Supervises the YT Library web service and worker queue for $repoRoot" `
            -StartupType AutomaticDelayedStart `
            -Credential $credential `
            -SecurityDescriptorSddl (Get-ServiceSecurityDescriptor)
        $created = $true
        Configure-ServiceRecovery
        $started = Invoke-Controller "start"
        [pscustomobject]@{
            Action = "install"
            Installed = $true
            ServiceName = $serviceName
            Account = $credential.UserName
            StartupType = "AutomaticDelayedStart"
            QueuePreserved = $queueShouldRun
            Controller = $started
        }
    }
    catch {
        if (-not $created -and $directWasRunning) {
            Set-QueueIntent $queueShouldRun "windows-service-install-rollback"
            $null = Invoke-Controller "start"
        }
        if ($created) {
            Write-Warning "The service remains installed so its SCM and .codex\service-logs diagnostics are preserved. Correct the account policy or password, then run update-credential."
        }
        throw
    }
    exit 0
}

if ($Action -eq "update-credential") {
    if ($null -eq (Get-WindowsServiceRecord)) {
        throw "Windows service $serviceName is not installed"
    }
    $credential = Get-ServiceCredential
    Add-LogOnAsServiceRight
    $previous = Invoke-Controller "status"
    $queueShouldRun = [bool]$previous.QueueRunning -or [bool]$previous.QueueResumePending
    $null = Invoke-Controller "stop"
    Set-Service -Name $serviceName -Credential $credential -ErrorAction Stop
    Set-QueueIntent $queueShouldRun "windows-service-credential-update"
    $started = Invoke-Controller "start"
    [pscustomobject]@{
        Action = "update-credential"
        ServiceName = $serviceName
        Account = $credential.UserName
        QueuePreserved = $queueShouldRun
        Controller = $started
    }
    exit 0
}

$service = Get-WindowsServiceRecord
if ($null -eq $service) {
    throw "Windows service $serviceName is not installed"
}
$previous = Invoke-Controller "status"
$queueShouldRun = [bool]$previous.QueueRunning -or [bool]$previous.QueueResumePending
$null = Invoke-Controller "stop"
Remove-Service -Name $serviceName
Wait-ForServiceRemoval
Set-QueueIntent $queueShouldRun "windows-service-uninstall"
$started = Invoke-Controller "start"
[pscustomobject]@{
    Action = "uninstall"
    Installed = $false
    ServiceName = $serviceName
    DirectModeRestored = $true
    QueuePreserved = $queueShouldRun
    Controller = $started
}
