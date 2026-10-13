# Exercises Invoke-GammaHotkey from the shipped watchdog in isolation.
#
# Alt+1 / Alt+2 switch the HDR association between the working pair. After Restore
# Windows Profile they must leave Windows' own profile alone until the GUI applies again.
#
# The native layer is a stub type defined here under the real name. The real one is
# compiled from the payload's C# source, which the harness never extracts, so nothing in
# this process can reach mscms. Nothing touches a real profile, association or task.

param([Parameter(Mandatory = $true)][string]$FunctionsPath)

$ErrorActionPreference = 'Stop'   # matches the watchdog itself
$script:fail = 0
$script:Log = New-Object System.Collections.Generic.List[string]
function Write-Log { param([string]$Message) $script:Log.Add($Message) }
$script:LastLogOnce = @{}

Add-Type -TypeDefinition @'
namespace ColorProfileWatchdog
{
    public static class Native
    {
        public const int CPST_EXTENDED_DISPLAY_COLOR_MODE = 8;
        public static System.Collections.Generic.List<string> Calls = new System.Collections.Generic.List<string>();
        public static int SetCurrentUserDefault(object display, int subtype, string profileName)
        {
            Calls.Add(profileName);
            return 0;
        }
        public static string GetDefaultProfileWithFallback(object display, int subtype) { return "stub"; }
    }
}
'@

$work = Join-Path ([System.IO.Path]::GetTempPath()) ('vhdrosd-wd-hk-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$script:StatePath = Join-Path $work 'State.json'
$script:GammaStatePath = Join-Path $work 'gamma_hotkeys.json'

$display = [pscustomobject]@{ GdiName = '\\.\DISPLAY1'; DevicePath = 'path-1' }
function Get-ActiveDisplays { return @($display) }
function Find-SavedDisplay { param($CurrentDisplay, $State = $script:state) return $State.Displays[0] }
function Format-HResult { param($Value) return [string]$Value }

. $FunctionsPath

function Reset-Case {
    param($Restored)
    [ColorProfileWatchdog.Native]::Calls.Clear()
    $script:Log.Clear()
    $script:GammaCacheStamp = $null
    $script:state = [pscustomobject]@{ Displays = @([pscustomobject]@{
        GdiName = '\\.\DISPLAY1'; DevicePath = 'path-1'
        WorkingOff = 'VOff.icm'; WorkingOn = 'VOn.icm'; GammaEnabled = $false; GammaUpdatedAt = '' }) }
    $script:state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:StatePath -Encoding UTF8
    $record = @{ gdi_name = '\\.\DISPLAY1'; device_path = 'path-1'; enabled = $false
                 updated_at = '2026-10-02T10:00:00+00:00'; active_profile = 'RealBase.icm'
                 profiles = @{ Off = 'RealBase.icm'; On = 'RealBase.icm' } }
    if ($null -ne $Restored) { $record.restored = $Restored }
    @{ displays = @{ d1 = $record } } | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $script:GammaStatePath -Encoding UTF8
}

function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host "PASS  $Name" }
    else { Write-Host "FAIL  $Name $Detail"; $script:fail++ }
}

function Get-Calls { return @([ColorProfileWatchdog.Native]::Calls) -join ',' }

# --- not restored: the hotkeys switch, as they always have -------------------------
# The control for every restored case below: the same setup, minus the mark.
foreach ($restored in @($null, $false)) {
    Reset-Case $restored
    Invoke-GammaHotkey -Enable $true
    Assert-That "not restored ($restored): Alt+1 switches to the On profile" ((Get-Calls) -eq 'VOn.icm') (Get-Calls)
    Assert-That "not restored ($restored): State.json records it" `
        ([bool]((Get-Content -Raw -LiteralPath $script:StatePath | ConvertFrom-Json).Displays[0].GammaEnabled))
    Invoke-GammaHotkey -Enable $false
    Assert-That "not restored ($restored): Alt+2 switches to the Off profile" ((Get-Calls) -eq 'VOn.icm,VOff.icm') (Get-Calls)
}

# --- restored: neither hotkey touches anything --------------------------------------
foreach ($enable in @($true, $false)) {
    Reset-Case $true
    $stateBefore = [System.IO.File]::ReadAllBytes($script:StatePath)
    $gammaBefore = [System.IO.File]::ReadAllBytes($script:GammaStatePath)
    Invoke-GammaHotkey -Enable $enable
    Assert-That "restored, Enable=${enable}: no association is changed" ((Get-Calls) -eq '') (Get-Calls)
    Assert-That "restored, Enable=${enable}: State.json is not stamped" `
        ([Convert]::ToBase64String($stateBefore) -eq [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($script:StatePath)))
    Assert-That "restored, Enable=${enable}: gamma_hotkeys.json keeps the restore" `
        ([Convert]::ToBase64String($gammaBefore) -eq [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($script:GammaStatePath)))
    Assert-That "restored, Enable=${enable}: the log says why" (($script:Log -join "`n") -like '*Gamma hotkey ignored on*restored*') ($script:Log -join ' | ')
}

Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue

if ($script:fail -eq 0) { Write-Host 'ALL PASS' } else { Write-Host "$($script:fail) FAILURE(S)" }
exit $script:fail
