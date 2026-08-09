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

# What Fail prints about the state of the machine. It starts as the truth
# before anything is registered, and the rollback path rewrites it. A stop
# message that is wrong about what it left behind is worse than no message.
$script:StateNote = 'No task was registered. Nothing was written to the POS or the cloud.'

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
    foreach ($line in ($script:StateNote -split "`n")) { Write-Host ('  ' + $line.Trim()) }
    Write-Host ('=' * 66)
    exit 1
}

function Get-PriorXml {
    <#
        A copy of the task as it stands, so a failure after registration can
        put it back. Export-ScheduledTask is Windows 8+; the customer machine
        runs SQL Server 2014 Express and may be older, so schtasks - which has
        existed since XP - is the fallback rather than the afterthought.
    #>
    param([Parameter(Mandatory = $true)][string]$Name)
    try {
        $exported = Export-ScheduledTask -TaskName $Name -ErrorAction Stop
        if ($exported) { return ($exported | Out-String) }
    }
    catch { }
    try {
        $raw = & schtasks.exe /Query /TN $Name /XML ONE 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) { return ($raw | Out-String) }
    }
    catch { }
    return $null
}

function Restore-Prior {
    <#
        Undo. Returns $true when the machine is back where it started.
        $null prior XML means there was no task before, so removing ours is
        the restoration.
    #>
    param([string]$Name, [string]$Xml)
    if ([string]::IsNullOrWhiteSpace($Xml)) {
        try {
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
            return (-not (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue))
        }
        catch { return $false }
    }
    try {
        $null = Register-ScheduledTask -TaskName $Name -Xml $Xml -Force -ErrorAction Stop
        return $true
    }
    catch { return $false }
}

function FailAndRollBack([string]$Name, [string]$Xml, [string]$What, [string]$Do) {
    $restored = Restore-Prior -Name $Name -Xml $Xml
    if ($restored) {
        $script:StateNote = if ([string]::IsNullOrWhiteSpace($Xml)) {
            "Rolled back: the task was removed again. This machine is exactly as`n" +
            'it was before this script ran. Nothing was written to the POS or the cloud.'
        } else {
            "Rolled back: the task that was here before has been put back exactly`n" +
            'as it was. Nothing was written to the POS or the cloud.'
        }
    } else {
        $script:StateNote =
            "!! COULD NOT ROLL BACK. A task named '$Name' may be registered and is`n" +
            "!! NOT the one we verified. Do not leave the machine like this.`n" +
            "!! Run: powershell -ExecutionPolicy Bypass -File .\install\uninstall_agent.ps1`n" +
            '!! then photograph this screen and call.'
    }
    Fail $What $Do
}

function Get-TaskXml {
    <#
        Built as a string rather than through the cmdlets so that what is
        registered is visible before it is registered, and identical on
        every Windows version.

        ---- why the repetition is on a TimeTrigger ----------------
        It used to be on the LogonTrigger, and that task NEVER RAN.

        A logon trigger fires on a logon *event*. Registering it while the
        user is already logged on does not produce one, and the repetition
        hangs off the trigger, so nothing repeats either. Measured on this
        machine, registered while logged on, no logoff:

            t+1m  LogonTrigger fired 0 time(s)   TimeTrigger fired 1
            t+2m  LogonTrigger fired 0 time(s)   TimeTrigger fired 2
            t+3m  LogonTrigger fired 0 time(s)   TimeTrigger fired 3
            t+4m  LogonTrigger fired 0 time(s)   TimeTrigger fired 4

            pos_probe_logon  LastRunTime=11/30/1999  LastTaskResult=267011
                             NextRunTime=            <- not even scheduled
            pos_probe_time   LastRunTime=6:35:35 PM  LastTaskResult=0

        267011 is SCHED_S_TASK_HAS_NOT_RUN. On a till that stays logged in
        for weeks the agent would have run zero times after we left, with
        a correctly registered task sitting there looking healthy.

        So: the TimeTrigger carries the repetition and starts at install
        time, and the LogonTrigger stays - without a repetition - purely so
        a cycle runs promptly after a reboot instead of waiting up to three
        minutes. One repetition, one job each, nothing competing.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$UserId,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$StartBoundary
    )

    $wrapper = Join-Path (Join-Path $Root 'install') 'run_agent.ps1'
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden ' +
                 ('-ExecutionPolicy Bypass -File "{0}" -Python "{1}"' -f $wrapper, $PythonPath)

    # Every value below comes from a path or a user name. An ampersand in
    # a folder name would otherwise produce XML the scheduler rejects with
    # a parse error nobody would connect to the folder it was installed
    # into. .NET's own escaper rather than a hand-rolled one.
    $eName  = [System.Security.SecurityElement]::Escape($Name)
    $eUser  = [System.Security.SecurityElement]::Escape($UserId)
    $eArgs  = [System.Security.SecurityElement]::Escape($arguments)
    $eRoot  = [System.Security.SecurityElement]::Escape($Root)
    $eStart = [System.Security.SecurityElement]::Escape($StartBoundary)

    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>POSentine agent. Reads the POS and uploads to the cloud. Never writes to the POS database.</Description>
    <URI>\$eName</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>$eStart</StartBoundary>
      <Repetition>
        <Interval>PT3M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$eUser</UserId>
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

# Seconds are truncated so the first repetition lands on a whole minute.
# A start boundary in the past is deliberate: the task is due the moment it
# is registered, which is what makes Phase E of the installer able to wait
# for a run rather than for a logon.
$startBoundary = (Get-Date).ToString('yyyy-MM-ddTHH:mm:00')

$xml = Get-TaskXml -Root $root -PythonPath $Python -UserId $userId `
                   -Name $TaskName -StartBoundary $startBoundary

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

# ---- snapshot first, so anything after this is reversible ----
$priorXml = $null
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host ("  [ .. ] a task named '" + $TaskName + "' already exists - replacing it")
    $priorXml = Get-PriorXml -Name $TaskName
    if ([string]::IsNullOrWhiteSpace($priorXml)) {
        Fail ("a task named '" + $TaskName + "' exists but its definition could not be read") `
             ("Without a copy of the task that is here now, this script cannot promise`n" +
              "to put it back if registration goes wrong - and a half-replaced task is`n" +
              "worse than one that was never touched. Remove it first:`n" +
              "  powershell -ExecutionPolicy Bypass -File .\install\uninstall_agent.ps1`n" +
              'then run this again.')
    }
    Write-Host '         (its current definition was saved, so a failure below is undone)'
}

try {
    # -Force replaces in one operation. Unregistering first would leave a
    # window with no task at all, and a failure inside that window would
    # leave the machine with nothing rather than with the old task.
    $null = Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force -ErrorAction Stop
}
catch {
    # Nothing was replaced - Register-ScheduledTask -Force is atomic - so
    # the plain Fail is accurate here and no rollback is needed.
    Fail ('could not register the task: ' + $_.Exception.Message) `
         ("If this says access is denied, the account cannot register tasks`n" +
          "even for itself, which is unusual and is a machine policy.`n" +
          "Do NOT work around it by running as SYSTEM or as another user:`n" +
          'the agent must run as the till account. Call.')
}

# ---- read it back -------------------------------------------
# Registering and reporting success are two different claims. This reads
# the scheduler's own copy and checks the things that would silently
# change behaviour if they were wrong. Every failure from here on rolls
# back: a task that is registered but wrong is the worst of the three
# possible outcomes, because it looks installed.
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    FailAndRollBack $TaskName $priorXml `
        'the task is not there after registering it' `
        'Registration reported success and produced nothing. Call.'
}

$problems = @()

# The repetition must sit on a trigger that fires without a logon. A logon
# trigger registered while the user is already logged on never fires - the
# logon event it waits for has already happened - so the task would sit
# there looking installed and run zero times. Measured; see Get-TaskXml.
$repeating = $task.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval }
if (-not $repeating) {
    $problems += 'no trigger carries a repetition - the agent would run once, or never'
} else {
    if ($repeating.Count -gt 1) {
        $problems += 'more than one trigger repeats - they would compete'
    }
    $repeat = @($repeating)[0]
    if ($repeat.Repetition.Interval -ne 'PT3M') {
        $problems += ("repetition interval is '" + $repeat.Repetition.Interval +
                      "', expected PT3M")
    }
    if ($repeat.Repetition.Duration) {
        $problems += ("repetition has a duration ('" + $repeat.Repetition.Duration +
                      "') - it must be indefinite")
    }
    if ($repeat.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger') {
        $problems += ('the repetition is on the logon trigger, which does not fire ' +
                      'while the user is already logged on - the agent would never run')
    }
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

# The scheduler's own answer to "when will this next run". Empty means it
# is not scheduled at all, which is exactly what the old logon-only trigger
# produced: registered, enabled, and never going to run.
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $info -or -not $info.NextRunTime) {
    $problems += ('the scheduler reports no NextRunTime - the task is registered ' +
                  'but not scheduled to run')
}

if ($problems.Count -gt 0) {
    FailAndRollBack $TaskName $priorXml ($problems -join "`n") `
        ("The scheduler stored something other than what we asked for.`n" +
         'Do not leave this running. Call.')
}

Write-Host ("  [ OK ] registered  1 task named '" + $TaskName + "' (read back and checked)")
Write-Host ('         trigger    every 3 minutes from ' + $startBoundary + ', indefinitely')
Write-Host '                    plus once at logon, so a reboot does not wait 3 minutes'
Write-Host ('         next run   ' + $info.NextRunTime)
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
Write-Host '  ! The task repeats every 3 minutes, but only while this user is'
Write-Host '    logged on. That is deliberate: running while logged off needs either'
Write-Host '    a stored password or an administrator to grant a logon right, and'
Write-Host '    this account has neither. If the till is ever logged out, cycles stop'
Write-Host '    until someone logs back in - nothing is lost, because the watermark'
Write-Host '    only ever moves forward. Tell the owner.'
Write-Host ('=' * 66)
exit 0
