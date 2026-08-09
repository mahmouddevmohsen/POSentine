<#
    run_agent.ps1 - what the Scheduled Task actually executes
    ============================================================
    The task cannot set environment variables by itself: a Scheduled
    Task action has a command, arguments and a working directory, and
    no environment block. This wrapper is that block.

    It is also why the action is powershell.exe rather than python.exe.
    A console application launched directly by the scheduler shows its
    window, and a black window appearing on the till every three minutes
    during service is not acceptable. PowerShell is started with
    -WindowStyle Hidden and python.exe inherits that hidden console.

    pythonw.exe would have removed the console entirely, and was rejected:
    under pythonw sys.stderr is None, agent.py's logging StreamHandler
    then fails on every record, and logging swallows the failure. Silent
    is the one thing this product may not be.

    The python path is passed in rather than resolved here, and rather
    than written into this file at install time: this file's sha256 is in
    MANIFEST.txt, and a script that rewrites itself would fail the
    integrity check preflight.bat runs before anything else.
    ============================================================
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Python
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root 'agent.log'

# The scheduler gives the action its own working directory, but a task
# edited by hand later might not. agent.py resolves config.json and
# state.json relative to the current directory, so this is load-bearing.
Set-Location -LiteralPath $root

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

try {
    & $Python 'agent.py' '--log' $log
    if ($null -eq $LASTEXITCODE) {
        # & did not run a process at all. Reporting 0 here would make a
        # task that never ran look like a task that succeeded.
        exit 1
    }
    exit $LASTEXITCODE
}
catch {
    # The task runs hidden, so this file is the only place this can be
    # seen. Never let a launch failure exit 0.
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Add-Content -LiteralPath $log -Encoding utf8 -Value `
        "$stamp ERROR   run_agent.ps1 could not start the agent: $($_.Exception.Message)"
    exit 1
}
