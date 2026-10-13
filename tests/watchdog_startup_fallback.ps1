$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$raw = Get-Content -Raw -LiteralPath (Join-Path $root '2- OPTIONAL - Install-Watchdog.bat')
$marker = ':__WATCHDOG_POWERSHELL_PAYLOAD__'
$source = $raw.Substring($raw.LastIndexOf($marker) + $marker.Length)
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
$clause=$ast.Find({param($n) $n -is [Management.Automation.Language.CatchClauseAst] -and $n.Body.Extent.Text -match '\$registrationError = \$_\.Exception\.Message'},$true)
if(-not $clause){throw 'Fallback catch not found'}
$block=[ScriptBlock]::Create($clause.Body.Statements.Extent.Text -join "`n")
function New-Item { [CmdletBinding()]param($Path,[switch]$Force) $script:created=$Path }
function New-ItemProperty {
 [CmdletBinding()]param($Path,$Name,$Value,$PropertyType,[switch]$Force)
 if($script:failRegistry){throw 'registry denied'}
 $script:written=@{path=$Path;name=$Name;value=$Value;kind=$PropertyType}
}
function Write-Warning { param($Message) $script:warning=$Message }
$wscriptPath='C:\Windows\System32\wscript.exe'
$LauncherPath='C:\Fixture Data\ColorProfileModeWatchdog\Launcher.vbs'
$script:failRegistry=$false
try { throw 'scheduler denied' } catch { . $block }
if($startupMethod -notlike 'HKCU Run*'){throw 'Fallback not recorded'}
if($script:written.name -ne 'ColorProfileModeWatchdog'){throw 'Wrong registry value'}
if($script:written.value -ne ('"{0}" //B //Nologo "{1}" /logon' -f $wscriptPath,$LauncherPath)){throw 'Paths not quoted or shared launcher missing'}
if($startupWarnings.Count -ne 1 -or -not $script:warning){throw 'Failure warning missing'}
$script:failRegistry=$true; $failed=$false
try { try { throw 'scheduler denied' } catch { . $block } } catch { $failed=$_.Exception.Message -like '*both failed*' }
if(-not $failed){throw 'Registry failure reported success'}
$script:failRegistry=$false; $script:written=$null; $LauncherPath='C:\'+('x'*300);$failed=$false
try { try { throw 'scheduler denied' } catch { . $block } } catch { $failed=$_.Exception.Message -like '*260 characters*' }
if(-not $failed -or $script:written){throw 'Long startup command accepted'}
Write-Output 'PASS: fallback quoting, warning, shared launcher, registry refusal and length bound; no registry writes'

# Exercise uninstaller's scheduler failure handling with fake COM only.
$uninstall=Get-Content -LiteralPath (Join-Path $root 'Uninstall-Watchdog.bat')
$line=$uninstall | Where-Object { $_ -match '\$connected=\$false' } | Select-Object -First 1
$body=$line.Trim().Substring(1); $body=$body.Substring(0,$body.Length-3)
$body=$body.Replace('[Runtime.InteropServices.Marshal]::ReleaseComObject($folder) | Out-Null','$null').Replace('[Runtime.InteropServices.Marshal]::ReleaseComObject($svc) | Out-Null','$null')
$uninstallBlock=[ScriptBlock]::Create($body)
$task='Virtual HDR OSD - Color Profile Mode Watchdog'
function New-Object {
 param($ComObject)
 if($script:denyConnect){throw 'scheduler unavailable'}
 $folder=[PSCustomObject]@{}
 $folder | Add-Member ScriptMethod GetTasks { param($flags) @([PSCustomObject]@{Name=$task}) }
 $folder | Add-Member ScriptMethod DeleteTask { param($name,$flags) throw 'task delete refused' }
 $service=[PSCustomObject]@{Folder=$folder}
 $service | Add-Member ScriptMethod Connect {}
 $service | Add-Member ScriptMethod GetFolder { param($name) $this.Folder }
 return $service
}
$script:denyConnect=$true;$fallback=$true
. $uninstallBlock
$fallback=$false;$failed=$false
try { . $uninstallBlock } catch { $failed=$true }
if(-not $failed){throw 'Unrecorded scheduler failure ignored'}
$script:denyConnect=$false;$fallback=$true;$failed=$false
try { . $uninstallBlock } catch { $failed=$_.Exception.Message -like '*task delete refused*' }
if(-not $failed){throw 'Task deletion failure ignored'}
Write-Output 'PASS: fallback uninstall works without scheduler but task deletion failures are not hidden'
