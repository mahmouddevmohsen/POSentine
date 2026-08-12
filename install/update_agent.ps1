<#
    update_agent.ps1 - the one-click POSentine updater
    ============================================================
    Double-click UPDATE_POSENTINE.bat -> this script. It turns the
    hand-run update procedure (STOP, backup, replace code, verify,
    start, wait for a natural cycle, confirm) into one pass that
    refuses to continue past a failed check.

    The customer machine is NOT a git repository. Updates arrive as
    posentine-<commit>.zip built by make_ship.py --zip. Everything
    below therefore moves files and never runs git.

    What is protected, unconditionally (these are never overwritten,
    never deleted, and the updater refuses to proceed if the artifact
    itself tries to contain them):

        config.json      the SQL password and the agent token
        state.json       the watermark; resetting it re-reads history
        agent.log        every cycle, rotated and capped by logsetup
        logs\            install transcripts
        state.lock       the agent's own cycle lock
        _backup\         this updater's own backups

    Backups: config.json is never copied - only its sha256 is recorded
    (F-1, 2026-08-12), so the SQL password and the agent token never sit
    at rest in _backup\. Every read or copy that can touch the LIVE
    agent.log uses FileShare::ReadWrite with a bounded retry (the 02:16
    OpenRead sharing-violation hardening).

    Phases (each stops on first failure, fail-closed):

        1 PRECHECK    locate + checksum the artifact, verify the live
                      install, config/state/log, task, python, disk
        2 BACKUP      copy the stateful files + every code file into
                      <install>\_backup\<timestamp>\
        3 STOP        install\uninstall_agent.ps1 (the sanctioned stop)
        4 UPDATE      extract the artifact into a staging dir, copy only
                      code onto the live install, verify MANIFEST + report.py
        5 PREFLIGHT   run preflight.py (the same logic preflight.bat runs;
                      the .bat itself ends with a keypress pause that would
                      hang an updater), require every gate to pass
        6 START       install\install_agent.ps1 (idempotent), then verify
                      the task exists, is enabled, and has a NextRunTime.
                      Never Start-ScheduledTask manually - the natural
                      trigger is the only proof that counts.
        7 MONITOR     watch agent.log + Get-ScheduledTaskInfo until one
                      natural cycle completes (LastTaskResult 0, LastRunTime
                      advanced, no ERROR/Traceback/FATAL in new log lines)
        8 CONFIRM     python agent.py --confirm, require RESULT: OK,
                      then show the tail of agent.log

    Failures before the backup never touch the agent. Failures after the
    backup restore the previous code + MANIFEST from _backup and, when the
    task was stopped, re-register it, so the machine returns to the last
    known-good state instead of staying half-updated.

    Rehearsal/test seams (used by test_update_agent.py, never by the bat):
      -SkipTaskOps          skip the task-exists precheck, STOP and START
      -SkipMonitor          skip the natural-cycle wait
      -PreflightTextFile    read preflight output from a file instead of
                            running preflight.py (verdict parsing is the
                            same either way)
      -ConfirmTextFile      read --confirm output from a file
      -MonitorTaskInfoFile  read task info from JSON instead of the
                            scheduler
      -PrecheckOnly         stop after Phase 1 and print the verdict
      -NoRollback           on failure, do not restore the backup

    This file is ASCII only and is saved with a UTF-8 BOM on purpose,
    exactly like install_agent.ps1 / run_agent.ps1: Windows PowerShell
    5.1 reads a BOM-less .ps1 as the system ANSI code page, and a single
    non-ASCII character becomes a parse error at a line that looks fine.
    ============================================================
#>

[CmdletBinding()]
param(
    # Where the live install lives. Default: the parent of this install\.
    [string]$InstallRoot = '',
    # Where the operator dropped the new posentine-*.zip.
    [string]$DownloadsDir = 'C:\Users\Techno\Downloads',
    # The scheduled task that IS the agent's loop.
    [string]$TaskName = 'thirdeyev',
    # Pin one artifact instead of taking the newest posentine-*.zip.
    [string]$ZipName = '',
    # Optional pin. When set, a mismatch stops before anything is touched.
    [string]$ExpectedSha256 = '',
    # Test seams (see header). Empty = run the real thing.
    [string]$PreflightTextFile = '',
    [string]$ConfirmTextFile = '',
    [string]$MonitorTaskInfoFile = '',
    [switch]$SkipTaskOps,
    [switch]$SkipMonitor,
    [switch]$PrecheckOnly,
    [switch]$NoRollback,
    # Dot-source seam: defines every function but does NOT run the update.
    # Used by test_update_agent.py to call the concurrent-safe file
    # helpers (Get-Sha256, Read-NewLogBytes, Get-PrefixSha256,
    # Copy-FileWithRetry, Get-LogTail) directly. Never set by the bat.
    [switch]$SkipRun,
    # Fail-closed limits.
    [int]$MinFreeMb = 200,
    [int]$MonitorTimeoutSeconds = 480,
    [int]$MonitorPollSeconds = 5
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------
# console + child-process encoding
# --------------------------------------------------------------------
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

if (-not $InstallRoot) {
    $InstallRoot = Split-Path -Parent $PSScriptRoot
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$DownloadsDir = [IO.Path]::GetFullPath($DownloadsDir).TrimEnd('\')
$LogDir = Join-Path $InstallRoot 'logs'
$LogPath = Join-Path $LogDir 'updater.log'

# What may never be overwritten, deleted, or shipped inside an artifact.
$script:Protected = @('config.json', 'state.json', 'agent.log',
                      'state.lock', 'logs', '_backup')

$script:Stage = 'INIT'
$script:BackupDir = ''
$script:RollbackPerformed = $false
$script:TaskWasStopped = $false

# --------------------------------------------------------------------
# logging
# --------------------------------------------------------------------
function Write-Log {
    param([string]$Text)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$stamp] $Text"
    Write-Host $line
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) {
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding utf8
    }
    catch {
        # The log must never be the thing that stops the updater.
        Write-Host "[$stamp] (could not write $LogPath : $($_.Exception.Message))"
    }
}

function Write-LogBlock {
    param([string]$Name, [string]$Text)
    Write-Log ("{0} : begin" -f $Name)
    if ($Text) {
        foreach ($ln in ($Text -split "`r?`n")) {
            Write-Log ("  {0} | {1}" -f $Name, $ln)
        }
    }
    Write-Log ("{0} : end" -f $Name)
}

# --------------------------------------------------------------------
# fail-closed failure path
# --------------------------------------------------------------------
function Fail {
    param(
        [string]$Stage,
        [string]$What,
        [string]$Do,
        [switch]$FromRollback
    )
    Write-Log ("FAILED at {0} : {1}" -f $Stage, $What)

    # Nothing to restore, or the restore itself failed: say so either way.
    if ($script:BackupDir -and -not $NoRollback -and -not $FromRollback) {
        try {
            Restore-Backup -Stage $Stage
            $script:RollbackPerformed = $true
        }
        catch {
            Write-Log ("ROLLBACK FAILED : {0}" -f $_.Exception.Message)
        }
    }

    Write-Host ''
    Write-Host ('=' * 66)
    Write-Host '  POSentine UPDATE FAILED'
    Write-Host ('=' * 66)
    Write-Host ('  Stage:      {0}' -f $Stage)
    Write-Host ('  Reason:     {0}' -f $What)
    Write-Host ('  Rollback:   {0}' -f $(if ($script:RollbackPerformed) {
        'performed - previous code and MANIFEST restored'
    } elseif ($script:BackupDir) {
        'not performed (-NoRollback)'
    } else {
        'not needed - nothing was modified'
    }))
    Write-Host ('  Log:        {0}' -f $LogPath)
    Write-Host ('  Backup:     {0}' -f $(if ($script:BackupDir) { $script:BackupDir } else { 'none' }))
    Write-Host ''
    Write-Host '  Do not touch anything. Send updater.log.'
    Write-Host ('=' * 66)
    if ($Do) { Write-Host ''; Write-Host $Do }
    exit 1
}

# --------------------------------------------------------------------
# Phase 1 helpers
# --------------------------------------------------------------------
# --------------------------------------------------------------------
# concurrent-safe file access - the 02:16 production hardening
# --------------------------------------------------------------------
# The agent opens agent.log with shared read/write while it appends. Any
# updater read of that LIVE log must request the same sharing and must
# retry a bounded number of times: a transient sharing violation (a cycle
# mid-append, log rotation) must not fail an update. A persistent refusal
# still re-raises - failures stay real and the updater fails closed.
# Static artifacts (the release zip, MANIFEST, report.py) never race with
# the agent, but they use the same helpers: FileShare::ReadWrite is
# harmless when nobody else is writing, and one proven code path beats two
# that must each be rehearsed.

function Invoke-WithRetry {
    param(
        [scriptblock]$Action,
        [string]$What,
        [int]$Attempts = 5,
        [int]$DelayMs = 500
    )
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            return & $Action
        }
        catch {
            if ($i -eq $Attempts) {
                Write-Log ("RETRY : {0} failed after {1} attempts - failing closed" -f $What, $Attempts)
                throw
            }
            Write-Log ("RETRY : {0} transient ({1}/{2}): {3}" -f $What, $i, $Attempts, $_.Exception.Message)
            Start-Sleep -Milliseconds $DelayMs
        }
    }
}

function Get-Sha256 {
    param([string]$Path)
    # Explicit shared open + bounded retry. Get-FileHash is fine for
    # static files, but agent.log is LIVE and a reader that does not ask
    # for the same sharing the agent uses is exactly what failed on the
    # till at 02:16 ("being used by another process").
    return Invoke-WithRetry -What ("hash {0}" -f $Path) -Action {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
                              [IO.FileShare]::ReadWrite)
        try {
            $sha = [Security.Cryptography.SHA256]::Create()
            try {
                $hash = $sha.ComputeHash($fs)
                return ([BitConverter]::ToString($hash) -replace '-', '').ToLower()
            }
            finally { $sha.Dispose() }
        }
        finally {
            if ($fs) { $fs.Dispose() }
        }
    }
}

function Read-NewLogBytes {
    param([string]$Path, [long]$Offset, [long]$Count)
    # Shared read + bounded retry, UTF-8 decoded. Get-Content would read
    # the system ANSI code page and mangle the Arabic report text (the
    # mojibake finding). A persistent refusal re-raises (fails closed).
    return Invoke-WithRetry -What ("read new log bytes of {0}" -f $Path) -Action {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
                              [IO.FileShare]::ReadWrite)
        try {
            $fs.Position = $Offset
            $buf = New-Object byte[] $Count
            $read = 0
            while ($read -lt $Count) {
                $n = $fs.Read($buf, $read, [int]($Count - $read))
                if ($n -le 0) { break }
                $read += $n
            }
            return [Text.Encoding]::UTF8.GetString($buf, 0, $read)
        }
        finally {
            if ($fs) { $fs.Dispose() }
        }
    }
}

function Get-PrefixSha256 {
    param([string]$Path, [long]$Length)
    # sha256 of ONLY the first $Length bytes, read with shared access: a
    # live log may legitimately grow after it was backed up, and the bytes
    # that existed at backup time are the ones that must be unchanged.
    return Invoke-WithRetry -What ("hash the first {0} bytes of {1}" -f $Length, $Path) -Action {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
                              [IO.FileShare]::ReadWrite)
        try {
            $buf = New-Object byte[] $Length
            $read = 0
            while ($read -lt $Length) {
                $n = $fs.Read($buf, $read, [int]($Length - $read))
                if ($n -le 0) { break }
                $read += $n
            }
            $sha = [Security.Cryptography.SHA256]::Create()
            try {
                $hash = $sha.ComputeHash($buf, 0, $read)
                return ([BitConverter]::ToString($hash) -replace '-', '').ToLower()
            }
            finally { $sha.Dispose() }
        }
        finally {
            if ($fs) { $fs.Dispose() }
        }
    }
}

function Copy-FileWithRetry {
    param([string]$Source, [string]$Destination, [string]$What)
    # Copy-Item on a LIVE log (backup runs before the task is stopped):
    # absorb a transient sharing violation, re-raise on a persistent one.
    Invoke-WithRetry -What $What -Action {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
}

function Get-LogTail {
    param([string]$Path, [int]$Lines = 30)
    # Display-only helper for the success screen. Shared read + UTF-8;
    # callers wrap this in try/catch - a live log that cannot be read
    # right now must never fail an update that already passed every gate.
    $all = Read-NewLogBytes -Path $Path -Offset 0 -Count (Get-Item -LiteralPath $Path).Length
    $kept = @(($all -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last $Lines)
    return ($kept -join "`n")
}

function Find-Artifact {
    if ($ZipName) {
        $candidate = Join-Path $DownloadsDir $ZipName
        if (-not (Test-Path -LiteralPath $candidate)) {
            Fail 'PRECHECK' "the pinned artifact was not found: $candidate" `
                "Copy $ZipName into $DownloadsDir and run again."
        }
        return Get-Item -LiteralPath $candidate
    }
    $zips = Get-ChildItem -LiteralPath $DownloadsDir -Filter 'posentine-*.zip' `
        -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
    if (-not $zips) {
        Fail 'PRECHECK' "no posentine-*.zip in $DownloadsDir" `
            'Copy the new release zip into Downloads and run again.'
    }
    if ($zips.Count -gt 1) {
        Write-Log ("PRECHECK : {0} artifacts found, newest selected:" -f $zips.Count)
        foreach ($z in $zips) { Write-Log ("PRECHECK :   {0}  ({1})" -f $z.Name, $z.LastWriteTime) }
    }
    return $zips[0]
}

function Test-SufficientDisk {
    $drive = (Get-Item -LiteralPath $InstallRoot).PSDrive
    $freeMb = [math]::Round($drive.Free / 1MB, 0)
    if ($freeMb -lt $MinFreeMb) {
        Fail 'PRECHECK' ("free disk space {0} MB is below {1} MB" -f $freeMb, $MinFreeMb) `
            'Free up space on this drive and run again.'
    }
    Write-Log ("PRECHECK : free disk {0} MB >= {1} MB" -f $freeMb, $MinFreeMb)
}

function Test-TaskExists {
    if ($SkipTaskOps) {
        Write-Log 'PRECHECK : task check skipped (rehearsal)'
        return
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Fail 'PRECHECK' "the scheduled task '$TaskName' is not registered" `
            "This update stops the agent to replace its code. If the task is" +
            ' already missing the agent is not running; install it first.'
    }
    Write-Log ("PRECHECK : task '{0}' present (state {1})" -f $TaskName, $task.State)
}

function Test-Python {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Fail 'PRECHECK' 'no python on PATH' `
            'The agent runs under Python; a machine without python cannot be updated.'
    }
    Write-Log ("PRECHECK : python {0}" -f $python)
    return $python
}

# --------------------------------------------------------------------
# Phase 2 + rollback
# --------------------------------------------------------------------
function New-Backup {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $script:BackupDir = Join-Path $InstallRoot ("_backup\{0}" -f $stamp)
    New-Item -ItemType Directory -Path $script:BackupDir -Force | Out-Null

    # config.json: the SQL password and the agent token. It is never
    # modified by an update (it is protected and refused from every
    # artifact), so rollback never needs a plaintext copy of it - only
    # proof it did not change. Record its sha256 instead of the file:
    # credentials must not sit at rest in _backup\ (F-1).
    $config = Join-Path $InstallRoot 'config.json'
    if (Test-Path -LiteralPath $config) {
        Set-Content -LiteralPath (Join-Path $script:BackupDir 'config.json.sha256') `
            -Value (Get-Sha256 $config) -Encoding ascii
        Write-Log 'BACKUP : config.json sha256 recorded (file itself not copied)'
    }
    else {
        Write-Log 'BACKUP : config.json MISSING in live install'
    }

    # state.json and agent.log are copied as files. agent.log is LIVE here
    # (backup runs before the task is stopped), so the copy goes through
    # Copy-FileWithRetry: a cycle appending at this moment is absorbed,
    # not fatal.
    $stateful = @('state.json', 'agent.log')
    foreach ($name in $stateful) {
        $src = Join-Path $InstallRoot $name
        if (Test-Path -LiteralPath $src) {
            Copy-FileWithRetry -Source $src -Destination (Join-Path $script:BackupDir $name) -What "backup $name"
            Write-Log ("BACKUP : {0}" -f $name)
        }
        else {
            Write-Log ("BACKUP : {0} MISSING in live install" -f $name)
        }
    }

    # Every code file the current MANIFEST lists: this is what a rollback
    # needs to restore the previous code, whatever the artifact changed.
    $manifest = Join-Path $InstallRoot 'MANIFEST.txt'
    if (Test-Path -LiteralPath $manifest) {
        $code = Join-Path $script:BackupDir 'code'
        New-Item -ItemType Directory -Path $code -Force | Out-Null
        foreach ($ln in (Get-Content -LiteralPath $manifest)) {
            $ln = $ln.Trim()
            if (-not $ln -or $ln.StartsWith('#')) { continue }
            $rel = ($ln -split '\s+', 2)[1].Trim()
            if (-not $rel) { continue }
            $src = Join-Path $InstallRoot $rel
            if (Test-Path -LiteralPath $src) {
                $dst = Join-Path $code $rel
                New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
                Copy-Item -LiteralPath $src -Destination $dst -Force
                Write-Log ("BACKUP : code\{0}" -f $rel)
            }
        }
        Copy-Item -LiteralPath $manifest -Destination $code -Force
        Write-Log 'BACKUP : code\MANIFEST.txt'
    }
    else {
        Write-Log 'BACKUP : no MANIFEST.txt to snapshot'
    }

    # Install transcripts, for history. Not the updater's own log.
    $logs = Join-Path $InstallRoot 'logs'
    if (Test-Path -LiteralPath $logs) {
        $dst = Join-Path $script:BackupDir 'logs'
        New-Item -ItemType Directory -Path $dst -Force | Out-Null
        Get-ChildItem -LiteralPath $logs -Filter 'install_*.txt' -ErrorAction SilentlyContinue |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $dst -Force }
        Write-Log 'BACKUP : logs\install_*.txt'
    }

    # What exactly was backed up, as a record in the backup itself.
    $record = (Get-ChildItem -LiteralPath $script:BackupDir -Recurse -File |
               ForEach-Object { $_.FullName.Substring($script:BackupDir.Length + 1) }) -join "`n"
    Set-Content -LiteralPath (Join-Path $script:BackupDir 'backup_list.txt') `
        -Value $record -Encoding utf8
    Write-Log ("BACKUP : created {0}" -f $script:BackupDir)
}

function Restore-Backup {
    param([string]$Stage)
    Write-Log ("ROLLBACK : restoring from {0}" -f $script:BackupDir)

    # Re-register the task first if we stopped it: the restore below puts
    # the OLD code back, and the task must point at it again.
    if ($script:TaskWasStopped -and -not $SkipTaskOps) {
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
                (Join-Path $InstallRoot 'install\install_agent.ps1') `
                -TaskName $TaskName
            if ($LASTEXITCODE -ne 0) {
                Write-Log ("ROLLBACK : task re-registration returned {0}" -f $LASTEXITCODE)
            }
            else {
                Write-Log 'ROLLBACK : task re-registered (old code)'
            }
        }
        catch {
            Write-Log ("ROLLBACK : task re-registration failed : {0}" -f $_.Exception.Message)
        }
    }

    # Code + MANIFEST first, then the stateful files (which are byte-identical
    # to the live copies unless something went very wrong - restoring them is
    # harmless and restores the exact pre-update state). Files the new
    # artifact introduced but the old MANIFEST did not list are left in place:
    # the shipped file list is fixed (make_ship.SHIPPED), so this cannot occur
    # today, and deleting unknowns is riskier than leaving inert files.
    $code = Join-Path $script:BackupDir 'code'
    if (Test-Path -LiteralPath $code) {
        Get-ChildItem -LiteralPath $code -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($code.Length + 1)
            $dst = Join-Path $InstallRoot $rel
            Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
            Write-Log ("ROLLBACK : restored {0}" -f $rel)
        }
    }
    # config.json is NOT restored: updates never modify it (only its
    # sha256 was recorded), so there is nothing to bring back. state.json
    # and agent.log restore the exact pre-update state; agent.log may be
    # held by a process at rollback time, so each stateful restore is
    # best-effort with the bounded retry - a file that cannot be written
    # is logged, never allowed to abort the code restore.
    foreach ($name in @('state.json', 'agent.log')) {
        $src = Join-Path $script:BackupDir $name
        if (Test-Path -LiteralPath $src) {
            try {
                Copy-FileWithRetry -Source $src -Destination (Join-Path $InstallRoot $name) -What "restore $name"
                Write-Log ("ROLLBACK : restored {0}" -f $name)
            }
            catch {
                Write-Log ("ROLLBACK : could not restore {0} (best-effort): {1}" -f $name, $_.Exception.Message)
            }
        }
    }
    Write-Log 'ROLLBACK : done'
}

# --------------------------------------------------------------------
# Phase 3
# --------------------------------------------------------------------
function Stop-Task {
    if ($SkipTaskOps) {
        Write-Log 'STOP : skipped (rehearsal)'
        return
    }
    Write-Log 'STOP : running install\uninstall_agent.ps1'
    $uninstall = Join-Path $InstallRoot 'install\uninstall_agent.ps1'
    if (-not (Test-Path -LiteralPath $uninstall)) {
        Fail 'STOP' "uninstall_agent.ps1 is missing ($uninstall)" `
            'The install folder is incomplete. Do not update it by hand.'
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uninstall -TaskName $TaskName
    if ($LASTEXITCODE -ne 0) {
        Fail 'STOP' ("uninstall_agent.ps1 exited {0}; the task may still be registered" -f $LASTEXITCODE) `
            'The agent could not be stopped safely. Nothing was updated.'
    }
    $still = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($still) {
        Fail 'STOP' "the task '$TaskName' is still registered after uninstall" `
            'Do not update files under a running task. Investigate and retry.'
    }
    $script:TaskWasStopped = $true
    Write-Log 'STOP : task removed and verified gone'
}

# --------------------------------------------------------------------
# Phase 4
# --------------------------------------------------------------------
function Test-ArtifactClean {
    param([string]$Staging)
    # An artifact that carries a protected name is defective: refuse it.
    # Every path COMPONENT is checked, not just the top level: the real
    # ship zip wraps everything in one posentine\ folder, and a protected
    # file hidden one level deep must not survive that wrapper.
    foreach ($entry in Get-ChildItem -LiteralPath $Staging -Recurse) {
        $parts = $entry.FullName.Substring($Staging.Length).TrimStart('\').Split('\')
        foreach ($part in $parts) {
            if ($script:Protected -contains $part) {
                Fail 'UPDATE' "the artifact contains a protected name: $part" `
                    'This zip was not produced by make_ship.py. Do not install it.'
            }
        }
    }
}

function Update-Code {
    $zip = Find-Artifact
    Write-Log ("UPDATE : artifact {0} ({1} bytes)" -f $zip.Name, $zip.Length)
    $sha = Get-Sha256 $zip.FullName
    Write-Log ("UPDATE : sha256 {0}" -f $sha)

    if ($ExpectedSha256) {
        if ($sha -ne $ExpectedSha256.ToLower()) {
            Fail 'PRECHECK' ("checksum mismatch`n  expected {0}`n  actual   {1}" -f `
                $ExpectedSha256.ToLower(), $sha) `
                'The zip is not the verified artifact. Nothing was modified and' +
                ' the agent was not stopped. Get the correct zip and retry.'
        }
        Write-Log 'PRECHECK : sha256 matches the configured expected value'
    }

    # Extract into a staging directory, never directly over the live install:
    # this is what makes "never overwrite the protected files" a guarantee
    # rather than a hope.
    $stage = Join-Path $env:TEMP ("posentine_update_{0}" -f ([guid]::NewGuid().ToString('N')))
    try {
        Expand-Archive -LiteralPath $zip.FullName -DestinationPath $stage -Force
        Write-Log ("UPDATE : extracted to staging {0}" -f $stage)
        Test-ArtifactClean $stage

        # The artifact root may be the staging dir itself or one posentine\
        # folder inside it (make_ship.py --zip prefixes every entry with
        # posentine/). Find whichever holds agent.py.
        $payload = $stage
        if (-not (Test-Path -LiteralPath (Join-Path $stage 'agent.py'))) {
            $nested = Join-Path $stage 'posentine'
            if (Test-Path -LiteralPath (Join-Path $nested 'agent.py')) {
                $payload = $nested
            }
            else {
                Fail 'UPDATE' 'the artifact has no agent.py at its root' `
                    'This zip is not a POSentine ship artifact.'
            }
        }

        # Copy code onto the live install. The protected names are not in the
        # payload (we refused above), so -Force cannot touch them.
        Get-ChildItem -LiteralPath $payload -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($payload.Length).TrimStart('\')
            # Second layer of the same guard: even if the scan above were
            # ever bypassed, no protected file is ever copied, and copying
            # onto one is a hard stop, not a skip.
            if ($script:Protected -contains $rel.Split('\')[0]) {
                Fail 'UPDATE' "the artifact contains a protected name: $rel" `
                    'This zip was not produced by make_ship.py. Do not install it.'
            }
            $dst = Join-Path $InstallRoot $rel
            New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
        }
        Write-Log 'UPDATE : code copied onto the live install'
    }
    finally {
        if (Test-Path -LiteralPath $stage) {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # The protected files must still be there, unchanged. Each is verified
    # the way its own concurrency allows:
    #   config.json - byte-exact against the sha256 recorded at backup
    #                 (the file itself is never stored - F-1);
    #   state.json  - byte-exact against its backup copy (atomic writes,
    #                 never concurrently modified);
    #   agent.log   - growth-tolerant prefix hash: a cycle in flight when
    #                 the task was stopped may legitimately append between
    #                 backup and here, so only the bytes that existed at
    #                 backup time must match; growth is expected, a shrink
    #                 is a hard fail.
    $configLive = Join-Path $InstallRoot 'config.json'
    if (-not (Test-Path -LiteralPath $configLive)) {
        Fail 'UPDATE' 'config.json disappeared during the update' 'Restore from the backup and investigate.'
    }
    $configShaFile = Join-Path $script:BackupDir 'config.json.sha256'
    if (Test-Path -LiteralPath $configShaFile) {
        $recorded = (Get-Content -LiteralPath $configShaFile -Raw).Trim().ToLower()
        if ((Get-Sha256 $configLive) -ne $recorded) {
            Fail 'UPDATE' 'config.json changed during the update - aborting' `
                'The artifact tried to touch a protected file. Rollback is automatic.'
        }
    }

    $stateLive = Join-Path $InstallRoot 'state.json'
    $stateBak = Join-Path $script:BackupDir 'state.json'
    if (-not (Test-Path -LiteralPath $stateLive)) {
        Fail 'UPDATE' 'state.json disappeared during the update' 'Restore from the backup and investigate.'
    }
    if ((Test-Path -LiteralPath $stateBak) -and
        (Get-Sha256 $stateLive) -ne (Get-Sha256 $stateBak)) {
        Fail 'UPDATE' 'state.json changed during the update - aborting' `
            'The artifact tried to touch a protected file. Rollback is automatic.'
    }

    $logLive = Join-Path $InstallRoot 'agent.log'
    $logBak = Join-Path $script:BackupDir 'agent.log'
    if (-not (Test-Path -LiteralPath $logLive)) {
        Fail 'UPDATE' 'agent.log disappeared during the update' 'Restore from the backup and investigate.'
    }
    if (Test-Path -LiteralPath $logBak) {
        $bakLen = (Get-Item -LiteralPath $logBak).Length
        if ((Get-PrefixSha256 -Path $logLive -Length $bakLen) -ne (Get-Sha256 $logBak)) {
            Fail 'UPDATE' 'agent.log changed during the update - aborting' `
                'The artifact tried to touch a protected file. Rollback is automatic.'
        }
        $liveLen = (Get-Item -LiteralPath $logLive).Length
        if ($liveLen -lt $bakLen) {
            Fail 'UPDATE' ("agent.log shrank during the update ({0} -> {1} bytes)" -f $bakLen, $liveLen) `
                'A protected file lost data. Rollback is automatic.'
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot 'logs'))) {
        Fail 'UPDATE' 'logs/ disappeared during the update' \
            'Restore from the backup and investigate.'
    }
    Write-Log 'UPDATE : protected files verified untouched'

    # MANIFEST must be the artifact's, and report.py must match it.
    $manifest = Join-Path $InstallRoot 'MANIFEST.txt'
    if (-not (Test-Path -LiteralPath $manifest)) {
        Fail 'UPDATE' 'MANIFEST.txt is missing after the update' `
            'This zip is not a POSentine ship artifact.'
    }
    $commitLine = (Get-Content -LiteralPath $manifest |
                   Where-Object { $_ -like '# built from:*' } | Select-Object -First 1)
    Write-Log ("UPDATE : manifest says {0}" -f ($commitLine.Trim() -replace '#', '').Trim())

    $want = $null
    foreach ($ln in (Get-Content -LiteralPath $manifest)) {
        $ln = $ln.Trim()
        if (-not $ln -or $ln.StartsWith('#')) { continue }
        $parts = $ln -split '\s+', 2
        if ($parts.Count -eq 2 -and $parts[1].Trim() -eq 'report.py') {
            $want = $parts[0].Trim().ToLower()
        }
    }
    if (-not $want) {
        Fail 'UPDATE' 'report.py is not listed in the new MANIFEST.txt'
    }
    $got = Get-Sha256 (Join-Path $InstallRoot 'report.py')
    if ($got -ne $want) {
        Fail 'UPDATE' ("report.py does not match MANIFEST.txt`n  want {0}`n  got  {1}" -f $want, $got) `
            'The copied code is not the verified artifact. Rollback is automatic.'
    }
    Write-Log 'UPDATE : report.py matches the new MANIFEST sha256'
}

# --------------------------------------------------------------------
# Phase 5
# --------------------------------------------------------------------
function Test-PreflightOutput {
    param([string]$Out, [int]$Code)
    if ($Code -ne 0) {
        return @{ Pass = $false; Why = "preflight exited $Code" }
    }
    # Markers from preflight.py's own success screen. The '?' in '1?4 PASSED'
    # is a -like wildcard that matches the en-dash preflight actually prints
    # ("steps 1-4 PASSED" with a hyphen here would never match); the wildcard
    # keeps this file ASCII-only. 'refused every write' is the read-only
    # proof's own line. Anything missing means a gate did not pass.
    foreach ($need in @('1?4 PASSED', '31 passed', 'refused every write',
                        'VERDICT: PASS')) {
        if ($Out -notlike "*$need*") {
            return @{ Pass = $false; Why = "preflight output lacks '$need'" }
        }
    }
    return @{ Pass = $true; Why = 'all preflight gates passed' }
}

function Run-Preflight {
    if ($PreflightTextFile) {
        Write-Log 'PREFLIGHT : reading verdict from test fixture'
        return @{ Out = (Get-Content -LiteralPath $PreflightTextFile -Raw); Code = 0 }
    }
    $python = Test-Python
    Write-Log 'PREFLIGHT : running preflight.py (steps 1-4, --skip-install)'
    $out = & $python (Join-Path $InstallRoot 'preflight.py') --skip-install 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-LogBlock 'PREFLIGHT' $out
    return @{ Out = $out; Code = $code }
}

# --------------------------------------------------------------------
# Phase 6
# --------------------------------------------------------------------
function Start-Task {
    if ($SkipTaskOps) {
        Write-Log 'START : skipped (rehearsal)'
        return
    }
    Write-Log 'START : running install\install_agent.ps1'
    $install = Join-Path $InstallRoot 'install\install_agent.ps1'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $install -TaskName $TaskName
    if ($LASTEXITCODE -ne 0) {
        Fail 'START' ("install_agent.ps1 exited {0}" -f $LASTEXITCODE) `
            'The task was not registered. Rollback is automatic.'
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Fail 'START' "the task '$TaskName' is not registered after install"
    }
    if ($task.State -ne 'Ready') {
        Fail 'START' ("the task state is '{0}', expected Ready" -f $task.State)
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $info -or -not $info.NextRunTime) {
        Fail 'START' 'the scheduler reports no NextRunTime' `
            'A registered task with no NextRunTime never runs.'
    }
    Write-Log ("START : task registered, next run {0}" -f $info.NextRunTime)
}

# --------------------------------------------------------------------
# Phase 7
# --------------------------------------------------------------------
function Get-TaskState {
    if ($MonitorTaskInfoFile) {
        $state = Get-Content -LiteralPath $MonitorTaskInfoFile -Raw | ConvertFrom-Json
        # ConvertFrom-Json in Windows PowerShell 5.1 leaves ISO-8601
        # timestamps as STRINGS (DateTime conversion is a PowerShell 7
        # behaviour). Normalise so the fixture and the real scheduler
        # path both carry a DateTime, and the display .ToString() works.
        if ($state -and $state.LastRunTime) {
            $state.LastRunTime = [datetime]$state.LastRunTime
        }
        return $state
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return $null }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        Present       = $true
        LastRunTime   = if ($info.LastRunTime) { $info.LastRunTime } else { [datetime]::MinValue }
        LastTaskResult = [int]$info.LastTaskResult
    }
}

function Test-CycleEvidence {
    param($State, [datetime]$Since, [string]$NewLog)
    if (-not $State -or -not $State.Present) {
        return @{ Pass = $false; Fatal = $true; Why = 'the task disappeared' }
    }
    $result = [int]$State.LastTaskResult
    # 267011 = SCHED_S_TASK_HAS_NOT_RUN (not yet), 267009 = SCHED_S_TASK_RUNNING.
    if ($result -ne 0 -and $result -ne 267011 -and $result -ne 267009) {
        return @{ Pass = $false; Fatal = $true;
                  Why = "LastTaskResult $result (non-zero failure)" }
    }
    if ($result -eq 0 -and $State.LastRunTime -gt $Since) {
        foreach ($bad in @('ERROR', 'Traceback', 'FATAL')) {
            if ($NewLog -like "*$bad*") {
                return @{ Pass = $false; Fatal = $true;
                          Why = "agent.log contains '$bad' after the update" }
            }
        }
        return @{ Pass = $true; Fatal = $false; Why = 'one natural cycle completed' }
    }
    return @{ Pass = $false; Fatal = $false; Why = 'waiting for the natural cycle' }
}

function Monitor-NaturalCycle {
    # Skipped when told to, or in a task-ops rehearsal with no fixture to
    # drive it (it would otherwise poll the real scheduler). A rehearsal
    # that passes -MonitorTaskInfoFile runs the monitor against the fixture.
    if ($SkipMonitor -or ($SkipTaskOps -and -not $MonitorTaskInfoFile)) {
        Write-Log 'MONITOR : skipped (rehearsal)'
        return
    }
    $logFile = Join-Path $InstallRoot 'agent.log'
    $since = Get-Date
    $deadline = $since.AddSeconds($MonitorTimeoutSeconds)
    # Bytes read so far: only NEW log lines are judged, so a stale ERROR or
    # Traceback from before the update can never fail the new agent.
    $lastLen = 0
    if (Test-Path -LiteralPath $logFile) {
        $lastLen = (Get-Item -LiteralPath $logFile).Length
    }

    Write-Host ''
    Write-Host ('=' * 66)
    Write-Host '  POSentine Update Monitor'
    Write-Host ('=' * 66)
    Write-Host ('  Task:   {0}' -f $TaskName)
    Write-Host '  Status: RUNNING - waiting for a NATURAL scheduled cycle'
    Write-Host '          (never started by hand - the trigger must fire it)'
    Write-Host ('=' * 66)

    while ((Get-Date) -lt $deadline) {
        $state = Get-TaskState
        $newLog = ''
        if (Test-Path -LiteralPath $logFile) {
            $len = (Get-Item -LiteralPath $logFile).Length
            if ($len -gt $lastLen) {
                # Shared read + bounded retry (the 02:16 fix): the agent
                # holds agent.log open while it appends, and OpenRead's
                # implicit FileShare.None is exactly what failed then.
                # A transient refusal is retried; a persistent one
                # re-raises and the update fails closed.
                $newLog = Read-NewLogBytes -Path $logFile -Offset $lastLen -Count ($len - $lastLen)
            }
            $lastLen = $len
        }
        $verdict = Test-CycleEvidence $state $since $newLog

        Write-Host ('  [{0}] LastRunTime={1}  LastTaskResult={2}  {3}' -f `
            (Get-Date -Format 'HH:mm:ss'), `
            $(if ($state -and $state.LastRunTime -gt [datetime]::MinValue) {
                $state.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '(never)' }),
            $(if ($state) { $state.LastTaskResult } else { '?' }),
            $verdict.Why)
        if ($newLog) {
            ($newLog -split "`r?`n") |
                Where-Object { $_.Trim() } | Select-Object -Last 3 |
                ForEach-Object { Write-Host ('      | {0}' -f $_.Trim()) }
        }
        Write-Log ("MONITOR : {0}" -f $verdict.Why)

        if ($verdict.Fatal) {
            Fail 'MONITOR' $verdict.Why `
                'The new agent failed its first natural cycle. Rollback is automatic.'
        }
        if ($verdict.Pass) {
            Write-Log 'MONITOR : natural cycle PASS'
            return
        }
        Start-Sleep -Seconds $MonitorPollSeconds
    }
    Fail 'MONITOR' ("no successful natural cycle within {0} seconds" -f $MonitorTimeoutSeconds) `
        'The task fired but never reported success. Rollback is automatic.'
}

# --------------------------------------------------------------------
# Phase 8
# --------------------------------------------------------------------
function Test-ConfirmOutput {
    param([string]$Out, [int]$Code)
    if ($Code -ne 0) {
        return @{ Pass = $false; Why = "--confirm exited $Code" }
    }
    if ($Out -notlike '*RESULT: OK*') {
        return @{ Pass = $false; Why = "--confirm did not report RESULT: OK" }
    }
    return @{ Pass = $true; Why = 'cloud confirmation OK' }
}

function Run-Confirm {
    if ($ConfirmTextFile) {
        Write-Log 'CONFIRM : reading verdict from test fixture'
        return @{ Out = (Get-Content -LiteralPath $ConfirmTextFile -Raw); Code = 0 }
    }
    $python = Test-Python
    Write-Log 'CONFIRM : running python agent.py --confirm'
    $out = & $python (Join-Path $InstallRoot 'agent.py') --confirm 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-LogBlock 'CONFIRM' $out
    return @{ Out = $out; Code = $code }
}

# --------------------------------------------------------------------
# the run
# --------------------------------------------------------------------
if (-not $SkipRun) {
    Write-Log ("UPDATE START : install={0} downloads={1}" -f $InstallRoot, $DownloadsDir)
Write-Log ("UPDATE START : task={0} artifact-pin={1} expected-sha={2}" -f `
    $TaskName, $(if ($ZipName) { $ZipName } else { 'newest' }),
    $(if ($ExpectedSha256) { 'configured' } else { 'none (MANIFEST is the gate)' }))
Write-Log ("UPDATE START : rehearsal switches: SkipTaskOps={0} SkipMonitor={1} PrecheckOnly={2}" -f `
    $SkipTaskOps, $SkipMonitor, $PrecheckOnly)

try {
    # ---- Phase 1 : precheck -------------------------------------
    $script:Stage = 'PRECHECK'
    foreach ($required in @('agent.py', 'config.json', 'state.json',
                            'agent.log', 'MANIFEST.txt')) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $required))) {
            Fail 'PRECHECK' "this does not look like a live install: $required is missing" `
                "Run the updater from the real install folder ($InstallRoot)."
        }
    }
    Write-Log 'PRECHECK : live install files present (agent.py, config.json, state.json, agent.log, MANIFEST.txt)'

    $zip = Find-Artifact
    Write-Log ("PRECHECK : selected {0}" -f $zip.FullName)
    $sha = Get-Sha256 $zip.FullName
    Write-Log ("PRECHECK : sha256 {0}" -f $sha)

    if ($ExpectedSha256) {
        if ($sha -ne $ExpectedSha256.ToLower()) {
            # Phase 1 failure: nothing has been touched, the agent is still
            # running on its old code. No backup, no rollback.
            Fail 'PRECHECK' ("checksum mismatch`n  expected {0}`n  actual   {1}" -f `
                $ExpectedSha256.ToLower(), $sha) `
                'The zip is not the verified artifact. The agent was NOT stopped and' +
                ' nothing was modified. Get the correct zip and retry.'
        }
        Write-Log 'PRECHECK : sha256 verified against the configured expected value'
    }
    else {
        Write-Log 'PRECHECK : no expected sha configured; MANIFEST verification in Phase 4 is the gate'
    }

    Test-SufficientDisk
    Test-TaskExists
    Test-Python

    if ($PrecheckOnly) {
        Write-Log 'PRECHECK : precheck-only mode - stopping here (nothing modified)'
        Write-Host ''
        Write-Host ('=' * 66)
        Write-Host '  POSentine PRECHECK OK - nothing was modified'
        Write-Host ('=' * 66)
        Write-Host ('  Artifact:  {0}' -f $zip.Name)
        Write-Host ('  Sha256:    {0}' -f $sha)
        Write-Host ('  Install:   {0}' -f $InstallRoot)
        Write-Host ('=' * 66)
        exit 0
    }

    # ---- Phase 2 : backup ---------------------------------------
    $script:Stage = 'BACKUP'
    New-Backup

    # ---- Phase 3 : stop -----------------------------------------
    $script:Stage = 'STOP'
    Stop-Task

    # ---- Phase 4 : update ---------------------------------------
    $script:Stage = 'UPDATE'
    Update-Code

    # ---- Phase 5 : preflight ------------------------------------
    $script:Stage = 'PREFLIGHT'
    $pf = Run-Preflight
    $verdict = Test-PreflightOutput $pf.Out $pf.Code
    if (-not $verdict.Pass) {
        Fail 'PREFLIGHT' $verdict.Why `
            'The new code failed its own acceptance gates. Rollback is automatic.'
    }
    Write-Log 'PREFLIGHT : PASS (integrity, golden tests, read-only proof, dry run)'

    # ---- Phase 6 : start ----------------------------------------
    $script:Stage = 'START'
    Start-Task

    # ---- Phase 7 : natural-cycle monitor ------------------------
    $script:Stage = 'MONITOR'
    Monitor-NaturalCycle

    # ---- Phase 8 : confirm --------------------------------------
    $script:Stage = 'CONFIRM'
    $cf = Run-Confirm
    $confirm = Test-ConfirmOutput $cf.Out $cf.Code
    if (-not $confirm.Pass) {
        Fail 'CONFIRM' $confirm.Why 'Rollback is automatic.'
    }
    Write-Log 'CONFIRM : RESULT: OK'

    # Display-only: the agent is running again, so agent.log is live. A
    # momentary inability to read it must never fail an update that
    # already passed every gate, so the tail is wrapped and non-fatal
    # (shared read + UTF-8 - the old Get-Content -Tail could both throw
    # on a locked log and mis-decode UTF-8 as ANSI).
    $tail = ''
    $logFile = Join-Path $InstallRoot 'agent.log'
    if (Test-Path -LiteralPath $logFile) {
        try {
            $tail = Get-LogTail -Path $logFile -Lines 30
        }
        catch {
            Write-Log ("AGENT.LOG TAIL could not be read (display only): {0}" -f $_.Exception.Message)
        }
    }
    if ($tail) { Write-LogBlock 'AGENT.LOG TAIL' $tail }

    # ---- success ------------------------------------------------
    $commit = ''
    $manifest = Join-Path $InstallRoot 'MANIFEST.txt'
    if (Test-Path -LiteralPath $manifest) {
        $commitLine = Get-Content -LiteralPath $manifest |
            Where-Object { $_ -like '# built from:*' } | Select-Object -First 1
        if ($commitLine) {
            $commit = ($commitLine -replace '^# built from:\s*', '').Trim()
        }
    }
    Write-Log 'UPDATE SUCCESS'
    Write-Host ''
    Write-Host ('=' * 66)
    Write-Host '  POSentine UPDATE SUCCESS'
    Write-Host ('=' * 66)
    Write-Host ('  Version/Commit:      {0}' -f $(if ($commit) { $commit } else { 'unknown' }))
    Write-Host ('  Artifact:            {0}' -f $zip.Name)
    Write-Host ('  Checksum:            {0}' -f $sha)
    Write-Host '  Preflight:           PASS'
    Write-Host '  Scheduled Task:      PASS'
    Write-Host '  Natural Agent Cycle: PASS'
    Write-Host '  Cloud Confirmation:  PASS'
    Write-Host '  Config:              PRESERVED'
    Write-Host '  State:               PRESERVED'
    Write-Host '  Customer Data:       UNTOUCHED'
    Write-Host ('  Log:                 {0}' -f $LogPath)
    Write-Host ('  Backup:              {0}' -f $script:BackupDir)
    Write-Host ('=' * 66)
    exit 0
}
catch {
    $msg = $_.Exception.Message
    Write-Log ("UNHANDLED ERROR at {0} : {1}" -f $script:Stage, $msg)

    # Same recovery as Fail: an unexpected exception AFTER the backup must
    # not leave the machine half-updated with the task stopped and the
    # agent down until a human intervenes.
    if ($script:BackupDir -and -not $NoRollback) {
        try {
            Restore-Backup -Stage $script:Stage
            $script:RollbackPerformed = $true
        }
        catch {
            Write-Log ("ROLLBACK FAILED : {0}" -f $_.Exception.Message)
        }
    }

    Write-Host ''
    Write-Host ('=' * 66)
    Write-Host '  POSentine UPDATE FAILED'
    Write-Host ('=' * 66)
    Write-Host ('  Stage:      {0}' -f $script:Stage)
    Write-Host ('  Reason:     {0}' -f $msg)
    Write-Host ('  Rollback:   {0}' -f $(if ($script:RollbackPerformed) {
        'performed - previous code and MANIFEST restored'
    } elseif ($script:BackupDir) {
        'not performed (-NoRollback)'
    } else {
        'not needed - nothing was modified'
    }))
    Write-Host ('  Log:        {0}' -f $LogPath)
    Write-Host ('=' * 66)
    exit 1
}
}
