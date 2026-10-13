# Exercises Write-LogOnce / Clear-LogOnce from the shipped watchdog in isolation.
#
# These decide how much a persistent fault writes to Watchdog.log. Get the answer wrong
# in one direction and a fault that lasts two hours erases the log's own history through
# the 512 KB rotation, taking the evidence of what caused it; wrong in the other and a
# fault that comes back is never mentioned again.
#
# Write-Log is stubbed to collect lines, so nothing here writes to a real log.

param([Parameter(Mandatory = $true)][string]$FunctionsPath)

$ErrorActionPreference = 'Stop'   # matches the watchdog itself
$script:fail = 0
$script:lines = @()

function Write-Log { param([string]$Message) $script:lines += $Message }

# The payload initialises this at the top level, outside any function, so the extractor
# cannot bring it across. tests/test_watchdog.py asserts separately that it is there.
$script:LastLogOnce = @{}

. $FunctionsPath

function Assert-Count {
    param([string]$What, [int]$Expected)
    if ($script:lines.Count -eq $Expected) {
        Write-Host "  ok    $What"
    } else {
        Write-Host "  FAIL  $What -- expected $Expected line(s), got $($script:lines.Count)"
        $script:fail++
    }
}

# A state that persists is reported once, however many passes see it. The reconcile path
# runs about 1.3 times a second per display, so this is the difference between one line
# and roughly 4,700 an hour.
$script:lines = @()
foreach ($pass in 1..500) {
    Write-LogOnce 'DISPLAY1|EXTENDED' 'Failed to restore EXTENDED profile on DISPLAY1: HRESULT 0x80070005'
}
Assert-Count 'five hundred identical passes write one line' 1

# A fault that changes is a different fault. The HRESULT is inside the message rather
# than the key precisely so this works.
Write-LogOnce 'DISPLAY1|EXTENDED' 'Failed to restore EXTENDED profile on DISPLAY1: HRESULT 0x80070002'
Assert-Count 'a different HRESULT is reported' 2

# Separate displays do not silence each other.
Write-LogOnce 'DISPLAY2|EXTENDED' 'Failed to restore EXTENDED profile on DISPLAY1: HRESULT 0x80070005'
Assert-Count 'a second display keeps its own key' 3

# After a success the condition is forgotten, so a recurrence is reported rather than
# deduped against a fault that has since been fixed.
Clear-LogOnce 'DISPLAY1|EXTENDED'
Write-LogOnce 'DISPLAY1|EXTENDED' 'Failed to restore EXTENDED profile on DISPLAY1: HRESULT 0x80070002'
Assert-Count 'a recurrence after recovery is reported again' 4

# Clearing a key that was never set must not throw -- the success branches call it on
# every ordinary pass, which is the overwhelmingly common case.
Clear-LogOnce 'DISPLAY9|NEVER-SET'
Assert-Count 'clearing an unknown key is harmless' 4

if ($script:fail -eq 0) { Write-Host 'ALL PASS' } else { Write-Host "$($script:fail) FAILURE(S)" }
exit $script:fail
