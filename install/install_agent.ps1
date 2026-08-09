<#
    install_agent.ps1 - VERIFY.md step 7
    ============================================================
    Registers the Scheduled Task that runs the agent. The task's
    3-minute repetition IS the agent's loop: agent.py performs one
    cycle per invocation and exits, so a dead process is replaced on
    the next tick and a reboot recovers by itself.

    User-level, never SYSTEM. The account on the customer machine is
    not an administrator, and registering a task for the current user
    does not require one. If registration fails on privileges this
    stops and says so - it does not fall back to something weaker.
    A task registered under the wrong principal is exactly the kind of
    silent difference that only shows up after we have left.

    Idempotent: running it twice replaces the task, never adds a second.

    Registration goes through explicit task XML rather than the
    New-ScheduledTaskTrigger cmdlets. An indefinite repetition built
    through those cmdlets depends on [TimeSpan]::MaxValue surviving a
    round trip into the task XML, which is version-dependent and has a
    known failure. XML with an <Interval> and no <Duration> means
    "forever" by definition, on every version, and can be inspected
    before it is registered:

        powershell -ExecutionPolicy Bypass -File .\install\install_agent.ps1 -ShowXml

    That prints exactly what would be registered and touches nothing.

    This file is ASCII only and is saved with a UTF-8 BOM on purpose.
    Windows PowerShell 5.1 reads a BOM-less .ps1 as the system ANSI code
    page, so a single non-ASCII character turns into a parse error at a
    line that looks fine - which is a poor way to meet this script.
    ============================================================
#>

[CmdletBinding()]
param(
    # Overridable so a second POS source on one machine, or a rehearsal,
    # does not collide with the live task.
    [string]$TaskName = 'thirdeyev',

    # Print the task XML and exit. Registers nothing, changes nothing.
    [switch]$ShowXml,

    # Normally resolved from PATH. Passed explicitly when the machine has
    # more than one Python and the wrong one is first.
    [string]$Python
)

$ErrorActionPreference = 'Stop'

$MinPythonMajor = 3
$MinPythonMinor = 11

function Fail([string]$What, [string]$Do) {
    Write-Host ''
    Write-Host ('=' * 66)
    Write-Host '  STOPPED - VERIFY.md step 7 (scheduled task)'
    Write-Host ('=' * 66)
    Write-Host ''
    Write-Host '  What failed:'
    foreach ($line in ($What -split "`n")) { Write-Host ('    ' + $line.Trim()) }
    Write-Host ''
    Write-Host '  What to do:'
    foreach ($line in ($Do -split "`n")) { Write-Host ('    ' + $line.Trim()) }
    Write-Host ''
    Write-Host '  No task was registered. Nothing was written to the POS or the cloud.'
    Write-Host ('=' * 66)
    exit 1
}

function Get-TaskXml {
    <#
        Built as a string rather than through the cmdlets so that what is
        registered is visible before it is registered, and identical on
        every Windows version.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$UserId,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $wrapper = Join-Path (Join-Path $Root 'install') 'run_agent.ps1'
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden ' +
                 ('-ExecutionPolicy Bypass -File "{0}" -Python "{1}"' -f $wrapper, $PythonPath)

    # Every value below comes from a path or a user name. An ampersand in
    # a folder name would otherwise produce XML the scheduler rejects with
    # a parse error nobody would connect to the folder it was installed
    # into. .NET's own escaper rather than a hand-rolled one.
    $eName = [System.Security.SecurityElement]::Escape($Name)
    $eUser = [System.Security.SecurityElement]::Escape($UserId)
    $eArgs = [System.Security.SecurityElement]::Escape($arguments)
    $eRoot = [System.Security.SecurityElement]::Escape($Root)

    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>POSentine agent. Reads the POS and uploads to the cloud. Never writes to the POS database.</Description>
    <URI>\$eName</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$eUser</UserId>
      <Repetition>
        <Interval>PT3M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$eUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$eArgs</Arguments>
      <WorkingDirectory>$eRoot</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

# ============================================================
# what we are installing
# ============================================================

$root = Split-Path -Parent $PSScriptRoot

Write-Host ('=' * 66)
Write-Host '  POSentine - install the scheduled task (VERIFY.md step 7)'
Write-Host ('=' * 66)
Write-Host ('  Folder:    ' + $root)
Write-Host ('  Task name: ' + $TaskName)
Write-Host ''

$agent = Join-Path $root 'agent.py'
if (-not (Test-Path -LiteralPath $agent)) {
    Fail ('agent.py is not in ' + $root) `
         ("This script must live in the install folder next to agent.py.`n" +
          'Re-copy the ship folder and run it from there.')
}

$wrapper = Join-Path $PSScriptRoot 'run_agent.ps1'
if (-not (Test-Path -LiteralPath $wrapper)) {
    Fail 'run_agent.ps1 is not next to this script' `
         'The copy onto this machine was incomplete. Re-copy the ship folder.'
}

# A task registered without config.json runs, fails, and writes an error
# to agent.log every three minutes forever. Refusing here is louder.
$config = Join-Path $root 'config.json'
if (-not $ShowXml -and -not (Test-Path -LiteralPath $config)) {
    Fail ('config.json is not in ' + $root) `
         ("Do VERIFY.md steps 1-6 first. Installing the task before the agent`n" +
          "has run a real cycle means it fails every three minutes, silently,`n" +
          'into agent.log.')
}

# ---- python -------------------------------------------------
if (-not $Python) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        Fail "no 'python' on PATH" `
             ("Install Python 3.11 or 3.12 and tick 'Add python.exe to PATH',`n" +
              "or pass the full path: -Python 'C:\Path\To\python.exe'")
    }
    $Python = $found.Source
}
if (-not (Test-Path -LiteralPath $Python)) {
    Fail ('python not found at ' + $Python) 'Pass a real path with -Python.'
}

# The task runs from a logon trigger, in an environment that is not this
# shell. Resolving python here and baking the full path into the task is
# what stops a PATH difference from producing a task that never runs.
# No double quotes inside the -c string: PowerShell strips them when it
# builds the command line for a native executable, and Python then sees
# print(%d.%d.%d % ...) and dies of a SyntaxError that looks like a broken
# interpreter rather than a quoting bug.
$versionText = (& $Python '-c' 'import sys;print(sys.version.split()[0])')
if ($LASTEXITCODE -ne 0) {
    Fail ('could not run ' + $Python + ' : ' + $versionText) `
         'That path is not a working Python interpreter.'
}
$parts = ([string]$versionText -split '\.')
if ([int]$parts[0] -lt $MinPythonMajor -or
    ([int]$parts[0] -eq $MinPythonMajor -and [int]$parts[1] -lt $MinPythonMinor)) {
    Fail ('Python ' + $versionText + ' is below the minimum ' +
          $MinPythonMajor + '.' + $MinPythonMinor) `
         'Install Python 3.11 or 3.12 and re-run.'
}
Write-Host ('  [ OK ] python      ' + $versionText)
Write-Host ('         ' + $Python)

$userId = $env:USERDOMAIN + '\' + $env:USERNAME
Write-Host ('  [ OK ] principal   ' + $userId +
            ' (user-level, LeastPrivilege - not SYSTEM)')

$xml = Get-TaskXml -Root $root -PythonPath $Python -UserId $userId -Name $TaskName

if ($ShowXml) {
    Write-Host ''
    Write-Host '  -ShowXml: printing the task XML. Nothing was registered.'
    Write-Host ('-' * 66)
    Write-Output $xml
    exit 0
}

# ============================================================
# register
# ============================================================

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host ("  [ .. ] a task named '" + $TaskName + "' already exists - replacing it")
}

try {
    # -Force replaces in one operation. Unregistering first would leave a
    # window with no task at all, and a failure inside that window would
    # leave the machine with nothing rather than with the old task.
    $null = Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force -ErrorAction Stop
}
catch {
    Fail ('could not register the task: ' + $_.Exception.Message) `
         ("If this says access is denied, the account cannot register tasks`n" +
          "even for itself, which is unusual and is a machine policy.`n" +
          "Do NOT work around it by running as SYSTEM or as another user:`n" +
          'the agent must run as the till account. Call.')
}

# ---- read it back -------------------------------------------
# Registering and reporting success are two different claims. This reads
# the scheduler's own copy and checks the things that would silently
# change behaviour if they were wrong.
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Fail 'the task is not there after registering it' `
         'Registration reported success and produced nothing. Call.'
}

$problems = @()
$interval = $task.Triggers[0].Repetition.Interval
if ($interval -ne 'PT3M') {
    $problems += ("repetition interval is '" + $interval + "', expected PT3M")
}
if ($task.Triggers[0].Repetition.Duration) {
    $problems += ("repetition has a duration ('" + $task.Triggers[0].Repetition.Duration +
                  "') - it must be indefinite")
}
if ($task.Principal.RunLevel -ne 'Limited') {
    $problems += ("principal RunLevel is '" + $task.Principal.RunLevel + "', expected Limited")
}
if ($task.Principal.UserId -notlike ('*' + $env:USERNAME)) {
    $problems += ("principal UserId is '" + $task.Principal.UserId + "', expected " + $userId)
}
if ($task.Actions[0].Execute -notlike '*powershell*') {
    $problems += ("action is '" + $task.Actions[0].Execute + "', expected powershell.exe")
}
if ($problems.Count -gt 0) {
    Fail ($problems -join "`n") `
         ("The scheduler stored something other than what we asked for.`n" +
          'Do not leave this running. Call.')
}

Write-Host ("  [ OK ] registered  1 task named '" + $TaskName + "' (read back and checked)")
Write-Host '         trigger    at logon, then every 3 minutes, indefinitely'
Write-Host '         action     powershell.exe -WindowStyle Hidden -> install\run_agent.ps1'
Write-Host '         env        PYTHONUTF8=1, PYTHONIOENCODING=utf-8 (set by run_agent.ps1)'
Write-Host '         limit      a cycle is killed after 15 minutes, which matches the'
Write-Host "                    agent's own stale-lock window so the two cannot disagree"
Write-Host ''
Write-Host ('=' * 66)
Write-Host '  Registered. Now finish VERIFY.md step 7:'
Write-Host ('=' * 66)
Write-Host ('    Get-ScheduledTask -TaskName ' + $TaskName + ' | Get-ScheduledTaskInfo')
Write-Host '    # expect LastTaskResult : 0'
Write-Host '    # wait three minutes, run it again, expect a newer LastRunTime,'
Write-Host '    # then: python agent.py --confirm - expect a NEWER heartbeat than step 6'
Write-Host ''
Write-Host '  ! The task runs at logon and only while this user is logged on.'
Write-Host '    That is deliberate: running while logged off needs either a stored'
Write-Host '    password or an administrator to grant a logon right, and this'
Write-Host '    account has neither. If the till is ever logged out, cycles stop'
Write-Host '    until someone logs back in. Tell the owner.'
Write-Host ('=' * 66)
exit 0
