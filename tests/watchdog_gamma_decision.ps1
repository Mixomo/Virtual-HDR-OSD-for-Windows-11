# Exercises Get-DesiredExtendedProfile from the shipped watchdog in isolation.
#
# The function decides which HDR profile Windows should be associated with, and a wrong
# answer silently reverts the user's correction setting. It cannot be reached without
# installing a scheduled task, so the test harness extracts it and stubs the native layer.
#
# Nothing here touches a real colour profile, association, or scheduled task.

param([Parameter(Mandatory = $true)][string]$FunctionsPath)

$ErrorActionPreference = 'Stop'   # matches the watchdog itself
$script:fail = 0
$script:FixtureWriteTick = [DateTime]::UtcNow
function Write-Log { param([string]$Message) }

# Write-LogOnce and Clear-LogOnce come from the payload alongside the functions under
# test, because Get-DesiredExtendedProfile calls them. This is the table they keep,
# which the payload initialises at the top level where the extractor cannot reach it.
$script:LastLogOnce = @{}

$colorDir = Join-Path ([System.IO.Path]::GetTempPath()) ('vhdrosd-wd-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $colorDir | Out-Null
foreach ($n in 'VOff.icm', 'VOn.icm', 'NewOff.icm', 'NewOn.icm', 'RealBase.icm', 'Vendor.icm') {
    Set-Content -LiteralPath (Join-Path $colorDir $n) -Value 'stub'
}

# Stub for the native helper the real script defines over mscms.
function Test-InstalledColorProfile {
    param([string]$ProfileName)
    if ([string]::IsNullOrWhiteSpace($ProfileName)) { return $false }
    return Test-Path -LiteralPath (Join-Path $colorDir $ProfileName)
}

$script:GammaStatePath = Join-Path ([System.IO.Path]::GetTempPath()) ('vhdrosd-wd-gamma-' + [guid]::NewGuid().ToString('N') + '.json')
. $FunctionsPath

function Set-Runtime {
    param($enabled, $updatedAt, $profiles = @{ Off = 'VOff.icm'; On = 'VOn.icm' }, $active = 'VOn.icm')
    @{ displays = @{ d1 = @{
        gdi_name = '\\.\DISPLAY1'; enabled = $enabled; updated_at = $updatedAt
        active_profile = $active; profiles = $profiles } } } |
        ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:GammaStatePath -Encoding UTF8
    $script:FixtureWriteTick = $script:FixtureWriteTick.AddMilliseconds(1)
    [IO.File]::SetLastWriteTimeUtc($script:GammaStatePath, $script:FixtureWriteTick)
}

function Set-RuntimeMulti {
    # Several records for ONE monitor, as accumulates when the adapter LUID is
    # reissued. Ordered stale-first, which is the order that used to be returned.
    param($records)
    $displays = [ordered]@{}
    $i = 0
    foreach ($r in $records) {
        $i++
        $displays["key$i"] = @{
            gdi_name = $script:TargetGdi; enabled = $r.enabled; updated_at = $r.updatedAt
            active_profile = $r.active; profiles = $r.profiles
        }
    }
    @{ displays = $displays } | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $script:GammaStatePath -Encoding UTF8
    $script:FixtureWriteTick = $script:FixtureWriteTick.AddMilliseconds(1)
    [IO.File]::SetLastWriteTimeUtc($script:GammaStatePath, $script:FixtureWriteTick)
}

function New-Saved {
    param($gammaEnabled, $updatedAt)
    $o = [pscustomobject]@{
        GdiName = '\\.\DISPLAY1'; StandardProfile = 'sRGB.icm'; ExtendedProfile = 'Base.icm'
        WorkingOff = 'VOff.icm'; WorkingOn = 'VOn.icm'; GammaEnabled = $gammaEnabled
    }
    if ($null -ne $updatedAt) {
        $o | Add-Member -NotePropertyName GammaUpdatedAt -NotePropertyValue $updatedAt -Force
    }
    return $o
}

$script:TargetGdi = '\\.\DISPLAY1'
$display = [pscustomobject]@{ GdiName = '\\.\DISPLAY1' }

function Assert-Profile {
    param($Name, $Expected, $Actual)
    if ($Expected -eq $Actual) {
        Write-Host "  PASS  $Name"
    } else {
        $script:fail++
        Write-Host "  FAIL  $Name (expected=$Expected actual=$Actual)"
    }
}

$old = (Get-Date).AddMinutes(-10).ToString('o')
$new = (Get-Date).ToString('o')

# The regression this function exists to prevent.
Set-Runtime -enabled $false -updatedAt $new -active 'VOff.icm'
Assert-Profile 'GUI turned correction off and is newer -> Off wins' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

# The watchdog must still win when it is the one that acted last.
Set-Runtime -enabled $true -updatedAt $old
Assert-Profile 'watchdog hotkey is newer -> its own choice wins' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $new))

# State.json written before GammaUpdatedAt existed.
Set-Runtime -enabled $true -updatedAt $new
Assert-Profile 'no GammaUpdatedAt -> GUI intent wins' 'VOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $null))

# GUI closed / never ran.
Remove-Item -LiteralPath $script:GammaStatePath -Force
Assert-Profile 'no runtime file, correction off -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))
Assert-Profile 'no runtime file, correction on -> captured state' 'VOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

# Malformed input must never throw under $ErrorActionPreference = 'Stop'.
Set-Content -LiteralPath $script:GammaStatePath -Value '{ not json' -Encoding UTF8
Assert-Profile 'corrupt runtime json -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))
Set-Runtime -enabled $true -updatedAt 'not-a-timestamp'
Assert-Profile 'unparseable timestamp -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))
Set-Runtime -enabled $true -updatedAt ''
Assert-Profile 'empty timestamp -> captured state' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))

# Never hand Windows a profile that is not installed. The newer intent still wins
# its DIRECTION though: falling back to the captured state here would answer with
# the opposite variant and undo what the user just asked for.
Set-Runtime -enabled $true -updatedAt $new -profiles @{ Off = 'Ghost_Off.icm'; On = 'Ghost_On.icm' }
Assert-Profile 'GUI names an uninstalled profile -> captured name, same direction' 'VOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))

# The GUI regenerates the pair under new filenames when the adapter LUID changes.
Set-Runtime -enabled $true -updatedAt $new -profiles @{ Off = 'NewOff.icm'; On = 'NewOn.icm' }
Assert-Profile 'adapter LUID changed -> GUI filenames preferred' 'NewOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $false $old))

# The GUI wrote naive local timestamps before they became offset-aware.
Set-Runtime -enabled $false -updatedAt (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss') -active 'VOff.icm'
Assert-Profile 'legacy naive timestamp still compares' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))


# One monitor with several records, from adapter-LUID reissues. Reading the first
# match handed back a stale record naming profiles that no longer exist, which
# silently defeated the whole intent comparison.
Set-RuntimeMulti @(
    @{ enabled = $true;  updatedAt = $old; active = 'VOn.icm';  profiles = @{ Off = 'Ghost_Off.icm'; On = 'Ghost_On.icm' } },
    @{ enabled = $false; updatedAt = $new; active = 'VOff.icm'; profiles = @{ Off = 'VOff.icm'; On = 'VOn.icm' } }
)
Assert-Profile 'stale duplicate record must not shadow the current one' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

# Newest wins regardless of enumeration order.
Set-RuntimeMulti @(
    @{ enabled = $false; updatedAt = $new; active = 'VOff.icm'; profiles = @{ Off = 'VOff.icm'; On = 'VOn.icm' } },
    @{ enabled = $true;  updatedAt = $old; active = 'VOn.icm';  profiles = @{ Off = 'Ghost_Off.icm'; On = 'Ghost_On.icm' } }
)
Assert-Profile 'newest record wins regardless of order' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

# No usable timestamp anywhere: must not crash, falls through to captured state.
Set-RuntimeMulti @(
    @{ enabled = $true;  updatedAt = 'bad'; active = 'VOn.icm';  profiles = @{ Off = 'Ghost_Off.icm'; On = 'Ghost_On.icm' } },
    @{ enabled = $false; updatedAt = '';    active = 'VOff.icm'; profiles = @{ Off = 'VOff.icm'; On = 'VOn.icm' } }
)
Assert-Profile 'unparseable timestamps everywhere -> captured state' 'VOn.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

# "Off is authoritative": when the requested variant is not installed, the answer
# must never be the opposite one.
Set-Runtime -enabled $false -updatedAt $new -profiles @{ Off = 'Ghost_Off.icm'; On = 'VOn.icm' } -active 'Ghost_Off.icm'
Assert-Profile 'Off requested, GUI Off missing -> captured Off, not On' 'VOff.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay (New-Saved $true $old))

$noOff = New-Saved $true $old
$noOff.WorkingOff = 'AlsoGone.icm'
Assert-Profile 'Off requested and nothing provides it -> change nothing' '' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $noOff)

# --- the captured fallback must also refuse uninstalled names ---------------
# The guard above covers the branch where the GUI's record is newer. These cases reach
# the captured Off/On pair instead, which needs it more rather than less: the names are
# recorded at install time and embed the adapter LUID, which Windows reissues across
# reboots, so they go stale by design. Naming a file that is gone makes the association
# write fail on every pass for as long as it lasts.
Remove-Item -LiteralPath $script:GammaStatePath -Force -ErrorAction SilentlyContinue

$goneOn = New-Saved $true $null
$goneOn.WorkingOn = 'NeverInstalled_On.icm'
Assert-Profile 'captured On is missing -> change nothing, never the opposite variant' '' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $goneOn)

$goneOff = New-Saved $false $null
$goneOff.WorkingOff = 'NeverInstalled_Off.icm'
Assert-Profile 'captured Off is missing -> change nothing, never the opposite variant' '' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $goneOff)

# With no pair captured at all there is no variant to get wrong, so the remaining
# candidates are tried in turn -- and the last of them is guarded too.
$noPair = New-Saved $false $null
$noPair.WorkingOff = ''
$noPair.WorkingOn = ''
$noPair.ExtendedProfile = 'VanishedBase.icm'
Assert-Profile 'no pair and an uninstalled captured base -> change nothing' '' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $noPair)

$noPairReal = New-Saved $false $null
$noPairReal.WorkingOff = ''
$noPairReal.WorkingOn = ''
$noPairReal.ExtendedProfile = 'Vendor.icm'
Assert-Profile 'no pair and an installed captured base -> that base' 'Vendor.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $noPairReal)

# --- Resolve-BaseExtendedProfile -------------------------------------------
# The captured HDR fallback must never be one of the app's own working profiles.
function New-Entry {
    param($base, $basePath)
    $o = [pscustomobject]@{ gdi_name = $script:TargetGdi }
    if ($null -ne $base)     { $o | Add-Member -NotePropertyName base_profile      -NotePropertyValue $base }
    if ($null -ne $basePath) { $o | Add-Member -NotePropertyName base_profile_path -NotePropertyValue $basePath }
    return $o
}

Assert-Profile 'a usable base name is adopted' 'RealBase.icm' `
    (Resolve-BaseExtendedProfile -GammaEntry (New-Entry 'RealBase.icm' $null) -CurrentExtended 'VOn.icm')

# Older builds published the ICC description here; its slashes make it no path at all.
Assert-Profile 'description in the name field falls back to the path' 'RealBase.icm' `
    (Resolve-BaseExtendedProfile `
        -GammaEntry (New-Entry 'HDR Calibrated Profile 8/14/2026 132247' (Join-Path $colorDir 'RealBase.icm')) `
        -CurrentExtended 'Virtual_HDR_OSD_abc1234567_Off.icm')

Assert-Profile 'an app-managed name is never adopted as the base' '' `
    (Resolve-BaseExtendedProfile `
        -GammaEntry (New-Entry 'Virtual_HDR_OSD_abc1234567_Off.icm' $null) `
        -CurrentExtended 'Virtual_HDR_OSD_abc1234567_Off.icm')

Assert-Profile 'no entry at all keeps a non-managed current association' 'Vendor.icm' `
    (Resolve-BaseExtendedProfile -GammaEntry $null -CurrentExtended 'Vendor.icm')

Assert-Profile 'no entry and a managed association reports none' '' `
    (Resolve-BaseExtendedProfile -GammaEntry $null -CurrentExtended 'Virtual_HDR_OSD_abc1234567_On.icm')

Assert-Profile 'an uninstalled base name is rejected' 'Vendor.icm' `
    (Resolve-BaseExtendedProfile -GammaEntry (New-Entry 'Gone.icm' $null) -CurrentExtended 'Vendor.icm')

# --- a restore published by the GUI ------------------------------------------
# Restore Windows Profile re-associates what Windows had, UNINSTALLS the working pair,
# and publishes the original as both variants. It relies on this function, unchanged,
# for every watchdog build already installed -- so these pin that reliance.
$restored = New-Saved $true $old
$restored.WorkingOff = 'Uninstalled_Off.icm'
$restored.WorkingOn  = 'Uninstalled_On.icm'
Set-Runtime -enabled $true -updatedAt $new -profiles @{ Off = 'RealBase.icm'; On = 'RealBase.icm' } -active 'RealBase.icm'
Assert-Profile 'restored: the original is asserted, whichever variant is wanted' 'RealBase.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $restored)
Set-Runtime -enabled $false -updatedAt $new -profiles @{ Off = 'RealBase.icm'; On = 'RealBase.icm' } -active 'RealBase.icm'
Assert-Profile 'restored, correction off: still the original' 'RealBase.icm' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $restored)

# The watchdog acted after the restore (its own hotkey): its captured pair wins the
# timestamp comparison, but that pair is uninstalled, so it must assert nothing.
$restoredOwnNewer = New-Saved $true $new
$restoredOwnNewer.WorkingOff = 'Uninstalled_Off.icm'
$restoredOwnNewer.WorkingOn  = 'Uninstalled_On.icm'
Set-Runtime -enabled $true -updatedAt $old -profiles @{ Off = 'RealBase.icm'; On = 'RealBase.icm' } -active 'RealBase.icm'
Assert-Profile 'restored, watchdog newer: its uninstalled pair is never re-asserted' '' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $restoredOwnNewer)

# Windows had no HDR profile at all: nothing to name, so nothing may be asserted.
Set-Runtime -enabled $true -updatedAt $new -profiles @{ Off = ''; On = '' } -active ''
Assert-Profile 'restored to no HDR profile: nothing is asserted' '' `
    (Get-DesiredExtendedProfile -CurrentDisplay $display -SavedDisplay $restored)

Remove-Item -LiteralPath $script:GammaStatePath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $colorDir -Recurse -Force -ErrorAction SilentlyContinue

if ($script:fail -eq 0) { Write-Host 'ALL PASS' } else { Write-Host "$($script:fail) FAILURE(S)" }
exit $script:fail
