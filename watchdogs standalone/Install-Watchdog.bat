@echo off
setlocal EnableExtensions
title Color Profile Mode Watchdog - Installer

set "APPDIR=%LOCALAPPDATA%\ColorProfileModeWatchdog"
set "WATCHDOG=%APPDIR%\Watchdog.ps1"
set "INSTALL_BAT=%~f0"
set "WATCHDOG_PATH=%WATCHDOG%"

echo.
echo Color Profile Mode Watchdog
echo Standalone installer - no Python, uv, or external app required.
echo.

if not exist "%APPDIR%" mkdir "%APPDIR%" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Could not create:
    echo   %APPDIR%
    pause
    exit 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$raw = Get-Content -Raw -LiteralPath $env:INSTALL_BAT; $marker=':__WATCHDOG_POWERSHELL_PAYLOAD__'; $i=$raw.LastIndexOf($marker); if($i -lt 0){throw 'Embedded watchdog payload was not found.'}; $payload=$raw.Substring($i+$marker.Length).TrimStart([char]13,[char]10); Set-Content -LiteralPath $env:WATCHDOG_PATH -Value $payload -Encoding UTF8" ^
  1>nul
if errorlevel 1 (
    echo ERROR: Could not extract the standalone watchdog.
    pause
    exit 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p=$env:WATCHDOG_PATH; $raw=Get-Content -Raw -LiteralPath $p; if(-not $raw.TrimStart().StartsWith('param(')){ throw 'Extracted watchdog is invalid.' }; if($raw -notmatch '\$nativeSource\s*=\s*@'''){ throw 'Embedded native API block is missing.' }" ^
  1>nul
if errorlevel 1 (
    echo ERROR: The extracted watchdog failed integrity validation.
    del /q "%WATCHDOG%" >nul 2>&1
    pause
    exit 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%WATCHDOG%" -Install
if errorlevel 1 (
    echo.
    echo Installation failed. Review the message above.
    pause
    exit 1
)

echo.
echo Installation complete.
echo The watchdog now runs invisibly at sign-in.
echo.
echo IMPORTANT:
echo If you intentionally change your SDR or HDR default profiles later,
echo run this installer again to capture the new associations.
echo.
pause
exit 0

:__WATCHDOG_POWERSHELL_PAYLOAD__
param(
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$AppDir = Join-Path $env:LOCALAPPDATA 'ColorProfileModeWatchdog'
$StatePath = Join-Path $AppDir 'State.json'
$LogPath = Join-Path $AppDir 'Watchdog.log'
$GammaStatePath = Join-Path $env:LOCALAPPDATA 'Virtual_HDR_OSD_for_Windows\gamma_hotkeys.json'
$LauncherPath = Join-Path $AppDir 'Launcher.vbs'
$ResultPath = Join-Path $AppDir 'install_result.json'
$TaskName = 'Virtual HDR OSD - Color Profile Mode Watchdog'

trap {
    $installFailure = $_
    if ($Install) {
        try {
            [IO.Directory]::CreateDirectory($AppDir) | Out-Null
            $result = @{ ok = $false; warnings = @($installFailure.Exception.Message); startup = ''; recorded_at = [DateTime]::UtcNow.ToString('o') }
            [IO.File]::WriteAllText($ResultPath, ($result | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        } catch {}
    }
    throw $installFailure
}

function Write-Log {
    param([string]$Message)

    try {
        if (Test-Path -LiteralPath $LogPath) {
            $item = Get-Item -LiteralPath $LogPath -ErrorAction SilentlyContinue
            if ($item -and $item.Length -gt 524288) {
                Move-Item -Force -LiteralPath $LogPath -Destination ($LogPath + '.old') -ErrorAction SilentlyContinue
            }
        }
        $line = '{0:yyyy-MM-dd HH:mm:ss.fff}  {1}' -f (Get-Date), $Message
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch {}
}

function Test-WatchdogSingletonHeld {
    # The only honest liveness check. The script being on disk, and a scheduled task
    # existing, both stay true after the watchdog has exited; and a process list is no
    # good either, because Win32_Process returns a null CommandLine for a process at a
    # higher integrity level than this one -- which is exactly the case that matters.
    try {
        $mutex = [System.Threading.Mutex]::OpenExisting('Local\ColorProfileModeWatchdogStandalone')
        try { $mutex.Dispose() } catch {}
        return $true
    } catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return $false
    } catch [System.UnauthorizedAccessException] {
        # It exists, and belongs to a process this one may not open: a watchdog started
        # by an elevated install. Answering "not running" here is what let an install
        # over one report that it had cleared the way.
        return $true
    } catch {
        return $false
    }
}

function Stop-ExistingWatchdog {
    try {
        $escaped = [Regex]::Escape((Join-Path $AppDir 'Watchdog.ps1'))
        $launcherPattern = [Regex]::Escape($LauncherPath)
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                ($_.Name -in @('powershell.exe','pwsh.exe','wscript.exe','cscript.exe')) -and
                $_.CommandLine -and
                (($_.CommandLine -match $escaped) -or ($_.CommandLine -match $launcherPattern)) -and
                ($_.ProcessId -ne $PID)
            } |
            Sort-Object @{ Expression = { if ($_.Name -in @('wscript.exe','cscript.exe')) { 0 } else { 1 } } } |
            ForEach-Object {
                $targetProcess = [Diagnostics.Process]::GetProcessById($_.ProcessId)
                try {
                    $result = Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction Stop
                    if ($result.ReturnValue -ne 0) { throw ("Could not stop watchdog PID " + $_.ProcessId + ": " + $result.ReturnValue) }
                    if (-not $targetProcess.WaitForExit(5000)) { throw 'Previous watchdog did not exit in time' }
                } finally { $targetProcess.Dispose() }
            }
    } catch { throw ('Could not stop the previous watchdog; installation stopped: ' + $_.Exception.Message) }
}

$nativeSource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace ColorProfileWatchdog
{
    public static class Native
    {
        static Native()
        {
            if (Marshal.SizeOf(typeof(DISPLAYCONFIG_PATH_SOURCE_INFO)) != 20 ||
                Marshal.SizeOf(typeof(DISPLAYCONFIG_PATH_TARGET_INFO)) != 48 ||
                Marshal.SizeOf(typeof(DISPLAYCONFIG_PATH_INFO)) != 72 ||
                Marshal.SizeOf(typeof(DISPLAYCONFIG_TARGET_DEVICE_NAME)) != 420)
                throw new InvalidOperationException("Invalid DisplayConfig ABI layout");
        }

        public const uint QDC_ONLY_ACTIVE_PATHS = 0x00000002;
        public const int ERROR_SUCCESS = 0;
        public const int ERROR_INSUFFICIENT_BUFFER = 122;

        public const int WCS_PROFILE_MANAGEMENT_SCOPE_SYSTEM_WIDE = 0;
        public const int WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER = 1;
        public const int CPT_ICC = 0;
        public const int CPST_STANDARD_DISPLAY_COLOR_MODE = 7;
        public const int CPST_EXTENDED_DISPLAY_COLOR_MODE = 8;

        public const int DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1;
        public const int DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9;
        public const int DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11;

        public const int HOTKEY_OFF = 0x5641;
        public const int HOTKEY_ON = 0x5642;
        public const int HOTKEY_FORCE_RESTORE = 0x5643;
        public const uint MOD_ALT = 0x0001;
        public const uint MOD_NOREPEAT = 0x4000;
        public const uint VK_1 = 0x31;
        public const uint VK_2 = 0x32;
        public const uint VK_3 = 0x33;
        public const uint WM_HOTKEY = 0x0312;
        public const uint PM_REMOVE = 0x0001;

        [StructLayout(LayoutKind.Sequential)]
        public struct LUID
        {
            public UInt32 LowPart;
            public Int32 HighPart;

            public override string ToString()
            {
                return HighPart.ToString("X8") + LowPart.ToString("X8");
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_RATIONAL
        {
            public UInt32 Numerator;
            public UInt32 Denominator;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_PATH_SOURCE_INFO
        {
            public LUID adapterId;
            public UInt32 id;
            public UInt32 modeInfoIdx;
            public UInt32 statusFlags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_PATH_TARGET_INFO
        {
            public LUID adapterId;
            public UInt32 id;
            public UInt32 modeInfoIdx;
            public Int32 outputTechnology;
            public Int32 rotation;
            public Int32 scaling;
            public DISPLAYCONFIG_RATIONAL refreshRate;
            public Int32 scanLineOrdering;

            [MarshalAs(UnmanagedType.Bool)]
            public bool targetAvailable;

            public UInt32 statusFlags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_PATH_INFO
        {
            public DISPLAYCONFIG_PATH_SOURCE_INFO sourceInfo;
            public DISPLAYCONFIG_PATH_TARGET_INFO targetInfo;
            public UInt32 flags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_DEVICE_INFO_HEADER
        {
            public Int32 type;
            public UInt32 size;
            public LUID adapterId;
            public UInt32 id;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct DISPLAYCONFIG_SOURCE_DEVICE_NAME
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;

            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
            public string viewGdiDeviceName;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct DISPLAYCONFIG_TARGET_DEVICE_NAME
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
            public UInt32 flags;
            public Int32 outputTechnology;
            public UInt16 edidManufactureId;
            public UInt16 edidProductCodeId;
            public UInt32 connectorInstance;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
            public string monitorFriendlyDeviceName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string monitorDevicePath;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
            public UInt32 value;
            public Int32 colorEncoding;
            public UInt32 bitsPerColorChannel;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
            public UInt32 value;
            public Int32 colorEncoding;
            public UInt32 bitsPerColorChannel;
            public UInt32 activeColorMode;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_SDR_WHITE_LEVEL
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
            public UInt32 SDRWhiteLevel;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct POINT
        {
            public Int32 X;
            public Int32 Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MSG
        {
            public IntPtr hwnd;
            public UInt32 message;
            public IntPtr wParam;
            public IntPtr lParam;
            public UInt32 time;
            public POINT pt;
        }

        public sealed class DisplayInfo
        {
            public string GdiName { get; set; }
            public string DevicePath { get; set; }
            public UInt32 AdapterLow { get; set; }
            public Int32 AdapterHigh { get; set; }
            public UInt32 SourceId { get; set; }
            public UInt32 TargetId { get; set; }
            public bool AdvancedColorEnabled { get; set; }
            public string Timing { get; set; }

            internal LUID AdapterLuid
            {
                get
                {
                    LUID value = new LUID();
                    value.LowPart = AdapterLow;
                    value.HighPart = AdapterHigh;
                    return value;
                }
            }
        }

        [DllImport("user32.dll")]
        private static extern int GetDisplayConfigBufferSizes(
            UInt32 flags,
            out UInt32 numPathArrayElements,
            out UInt32 numModeInfoArrayElements);

        [DllImport("user32.dll")]
        private static extern int QueryDisplayConfig(
            UInt32 flags,
            ref UInt32 numPathArrayElements,
            [Out] DISPLAYCONFIG_PATH_INFO[] pathArray,
            ref UInt32 numModeInfoArrayElements,
            IntPtr modeInfoArray,
            IntPtr currentTopologyId);

        [DllImport("user32.dll")]
        private static extern int DisplayConfigGetDeviceInfo(
            ref DISPLAYCONFIG_SOURCE_DEVICE_NAME requestPacket);

        [DllImport("user32.dll", EntryPoint = "DisplayConfigGetDeviceInfo")]
        private static extern int DisplayConfigGetAdvancedColorInfo(
            ref DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO requestPacket);

        [DllImport("user32.dll", EntryPoint = "DisplayConfigGetDeviceInfo")]
        private static extern int DisplayConfigGetSdrWhiteLevel(
            ref DISPLAYCONFIG_SDR_WHITE_LEVEL requestPacket);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

        [DllImport("user32.dll")]
        private static extern int GetMessage(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

        [DllImport("user32.dll")]
        private static extern bool PostThreadMessage(uint idThread, uint Msg, UIntPtr wParam, IntPtr lParam);

        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentThreadId();

        [DllImport("user32.dll")]
        private static extern bool PeekMessage(out MSG msg, IntPtr hwnd, uint min, uint max, uint remove);

        private const uint WM_QUIT = 0x0012;
        private const uint WM_RETRY_HOTKEYS = 0x8001;
        private static readonly object queueLock = new object();
        private static readonly Queue<int> gammaHotkeyQueue = new Queue<int>();
        private static readonly System.Threading.AutoResetEvent displayChangeSignal =
            new System.Threading.AutoResetEvent(false);
        private static readonly System.Threading.ManualResetEvent gammaHotkeyReady =
            new System.Threading.ManualResetEvent(false);
        private static System.Threading.Thread gammaHotkeyThread;
        private static volatile bool gammaHotkeysRegistered;
        private static uint gammaHotkeyThreadId;
        private static readonly System.Diagnostics.Stopwatch clock = System.Diagnostics.Stopwatch.StartNew();
        private static readonly RecoverySchedule recovery = new RecoverySchedule();
        private static System.Threading.Timer startupGuard;
        public static void BeginStartupGuard(string logPath)
        {
            startupGuard = new System.Threading.Timer(delegate(object ignored) {
                try { System.IO.File.AppendAllText(logPath, "Startup exceeded 25 seconds; exiting for bounded restart.\r\n"); } catch { }
                Environment.Exit(9);
            }, null, 25000, System.Threading.Timeout.Infinite);
        }
        public static void CompleteStartup()
        {
            if (startupGuard != null) { startupGuard.Dispose(); startupGuard = null; }
        }
        private static long pauseUntil;
        private static long nextProbe;
        private static long nextHotkeyRetry;

        // Fixed-size state and monotonic deadlines. Notification storms cannot
        // extend a window forever; a real mode/topology change replaces it.
        public sealed class RecoverySchedule
        {
            private string signature;
            private long started;
            private int attempt = 3;
            private static readonly long[] delays = { 700, 2000, 5000 };
            public void Observe(string value, long now)
            {
                if (signature == value) return;
                signature = value;
                Request(now);
            }
            public void Unavailable()
            {
                signature = null;
                attempt = 3;
            }
            public void Request(long now)
            {
                started = now;
                attempt = 0;
            }
            public bool Due(long now)
            {
                if (attempt >= 3 || now - started < delays[attempt]) return false;
                attempt++;
                return true;
            }
        }

        private static void OnDisplaySettingsChanged(object sender, EventArgs e)
        {
            displayChangeSignal.Set();
        }

        private static void OnUserPreferenceChanged(object sender, Microsoft.Win32.UserPreferenceChangedEventArgs e)
        {
            OnDisplaySettingsChanged(sender, e);
        }

        private static void EnqueueHotkey(int id)
        {
            lock (queueLock)
            {
                // Preserve the latest commands under a flood, cap retained memory.
                if (gammaHotkeyQueue.Count == 16) gammaHotkeyQueue.Dequeue();
                gammaHotkeyQueue.Enqueue(id);
            }
            displayChangeSignal.Set();
        }

        private static void GammaHotkeyMessageLoop()
        {
            gammaHotkeyThreadId = GetCurrentThreadId();
            MSG msg;
            PeekMessage(out msg, IntPtr.Zero, 0, 0, 0); // Create queue before publishing readiness.
            bool off = false, on = false, restore = false;
            try
            {
                try {
                    Microsoft.Win32.SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
                    Microsoft.Win32.SystemEvents.UserPreferenceChanged += OnUserPreferenceChanged;
                } catch { /* Native probes remain available without SystemEvents. */ }
                // Keep available keys alive, even if another app owns a different key.
                // Never register Win+Alt+B: Windows owns that shortcut.
                while (true)
                {
                    uint modifiers = MOD_ALT | MOD_NOREPEAT;
                    if (!off) off = RegisterHotKey(IntPtr.Zero, HOTKEY_OFF, modifiers, VK_1);
                    if (!on) on = RegisterHotKey(IntPtr.Zero, HOTKEY_ON, modifiers, VK_2);
                    if (!restore) restore = RegisterHotKey(IntPtr.Zero, HOTKEY_FORCE_RESTORE, modifiers, VK_3);
                    gammaHotkeysRegistered = off && on && restore;
                    gammaHotkeyReady.Set();
                    int result = GetMessage(out msg, IntPtr.Zero, 0, 0);
                    if (result <= 0) break;
                    if (msg.message == WM_HOTKEY)
                    {
                        int id = msg.wParam.ToInt32();
                        if (id == HOTKEY_OFF || id == HOTKEY_ON || id == HOTKEY_FORCE_RESTORE)
                            EnqueueHotkey(id);
                    }
                }
            }
            catch { gammaHotkeyReady.Set(); }
            finally
            {
                if (off) UnregisterHotKey(IntPtr.Zero, HOTKEY_OFF);
                if (on) UnregisterHotKey(IntPtr.Zero, HOTKEY_ON);
                if (restore) UnregisterHotKey(IntPtr.Zero, HOTKEY_FORCE_RESTORE);
                try {
                    Microsoft.Win32.SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;
                    Microsoft.Win32.SystemEvents.UserPreferenceChanged -= OnUserPreferenceChanged;
                } catch { }
                gammaHotkeysRegistered = false;
                gammaHotkeyThreadId = 0;
            }
        }

        public static bool HotkeyLoopReady { get { return gammaHotkeyReady.WaitOne(0); } }

        public static bool TryRegisterGammaHotkeys()
        {
            if (gammaHotkeysRegistered) return true;
            if (gammaHotkeyThread != null && gammaHotkeyThread.IsAlive)
            {
                if (clock.ElapsedMilliseconds >= nextHotkeyRetry)
                {
                    nextHotkeyRetry = clock.ElapsedMilliseconds + 2000;
                    PostThreadMessage(gammaHotkeyThreadId, WM_RETRY_HOTKEYS, UIntPtr.Zero, IntPtr.Zero);
                }
                return false;
            }
            if (clock.ElapsedMilliseconds < nextHotkeyRetry) return false;
            nextHotkeyRetry = clock.ElapsedMilliseconds + 2000;
            gammaHotkeyReady.Reset();
            gammaHotkeyThread = new System.Threading.Thread(GammaHotkeyMessageLoop);
            gammaHotkeyThread.IsBackground = true;
            gammaHotkeyThread.Name = "ColorProfileWatchdog-Hotkeys";
            gammaHotkeyThread.Start();
            gammaHotkeyReady.WaitOne(1500);
            return gammaHotkeysRegistered;
        }

        public static int PollGammaHotkey()
        {
            lock (queueLock)
                return gammaHotkeyQueue.Count > 0 ? gammaHotkeyQueue.Dequeue() : 0;
        }

        public static void PauseRecoveryFor(int milliseconds)
        {
            pauseUntil = clock.ElapsedMilliseconds + Math.Max(0, Math.Min(3000, milliseconds));
        }

        public static void RequestRecovery()
        {
            recovery.Request(clock.ElapsedMilliseconds);
        }

        public static bool WaitForDisplayChange(int timeoutMilliseconds)
        {
            displayChangeSignal.WaitOne(timeoutMilliseconds);
            long now = clock.ElapsedMilliseconds;
            if (now >= nextProbe)
            {
                nextProbe = now + 1000;
                try
                {
                    DisplayInfo[] displays = GetActiveDisplays();
                    // Stable order avoids spurious transitions when path order changes.
                    string[] keys = new string[displays.Length];
                    for (int i = 0; i < displays.Length; i++)
                    {
                        DisplayInfo d = displays[i];
                        keys[i] = d.AdapterHigh + ":" + d.AdapterLow + ":" + d.SourceId + ":" +
                            d.TargetId + ":" + d.AdvancedColorEnabled + ":" + d.Timing + ":" +
                            GetDefaultProfileWithFallback(d, d.AdvancedColorEnabled ?
                                CPST_EXTENDED_DISPLAY_COLOR_MODE : CPST_STANDARD_DISPLAY_COLOR_MODE);
                    }
                    Array.Sort(keys, StringComparer.Ordinal);
                    recovery.Observe(String.Join("|", keys), now);
                }
                catch { recovery.Unavailable(); /* Reacquisition starts a fresh window. */ }
            }
            if (pauseUntil > now) return false;
            if (pauseUntil != 0) { pauseUntil = 0; recovery.Request(now); }
            return recovery.Due(now);
        }

        public static void UnregisterGammaHotkeys()
        {
            uint threadId = gammaHotkeyThreadId;
            if (threadId != 0) PostThreadMessage(threadId, WM_QUIT, UIntPtr.Zero, IntPtr.Zero);
            if (gammaHotkeyThread != null && gammaHotkeyThread.IsAlive) gammaHotkeyThread.Join(1500);
            gammaHotkeysRegistered = false;
        }

        [DllImport("mscms.dll", CharSet = CharSet.Unicode)]
        private static extern bool GetColorDirectory(
            string pMachineName,
            System.Text.StringBuilder pBuffer,
            ref UInt32 pdwSize);

        public static string GetWindowsColorDirectory()
        {
            UInt32 size = 0;
            GetColorDirectory(null, null, ref size);
            if (size == 0)
                return System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "System32", "spool", "drivers", "color");

            System.Text.StringBuilder sb = new System.Text.StringBuilder((int)size);
            if (!GetColorDirectory(null, sb, ref size))
                return System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "System32", "spool", "drivers", "color");
            return sb.ToString();
        }

        [DllImport("mscms.dll", CharSet = CharSet.Unicode)]
        private static extern int ColorProfileGetDisplayDefault(
            int scope,
            LUID targetAdapterID,
            UInt32 sourceID,
            int profileType,
            int profileSubType,
            out IntPtr profileName);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode)]
        private static extern int ColorProfileSetDisplayDefaultAssociation(
            int scope,
            string profileName,
            int profileType,
            int profileSubType,
            LUID targetAdapterID,
            UInt32 sourceID);

        [DllImport("mscms.dll")]
        private static extern int ColorProfileGetDisplayUserScope(
            LUID targetAdapterID,
            UInt32 sourceID,
            out int scope);

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr hMem);

        private static string GetSourceName(DISPLAYCONFIG_PATH_SOURCE_INFO source)
        {
            DISPLAYCONFIG_SOURCE_DEVICE_NAME packet = new DISPLAYCONFIG_SOURCE_DEVICE_NAME();
            packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
            packet.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME));
            packet.header.adapterId = source.adapterId;
            packet.header.id = source.id;
            packet.viewGdiDeviceName = String.Empty;

            int rc = DisplayConfigGetDeviceInfo(ref packet);
            if (rc != ERROR_SUCCESS)
                return null;

            return packet.viewGdiDeviceName;
        }

        [DllImport("user32.dll", EntryPoint = "DisplayConfigGetDeviceInfo")]
        private static extern int DisplayConfigGetAdvancedColorInfo2(ref DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2 packet);

        [DllImport("user32.dll", EntryPoint = "DisplayConfigGetDeviceInfo")]
        private static extern int DisplayConfigGetTargetName(ref DISPLAYCONFIG_TARGET_DEVICE_NAME packet);

        private static string GetDevicePath(DISPLAYCONFIG_PATH_TARGET_INFO target)
        {
            DISPLAYCONFIG_TARGET_DEVICE_NAME packet = new DISPLAYCONFIG_TARGET_DEVICE_NAME();
            packet.header.type = 2;
            packet.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_TARGET_DEVICE_NAME));
            packet.header.adapterId = target.adapterId;
            packet.header.id = target.id;
            return DisplayConfigGetTargetName(ref packet) == 0 ? packet.monitorDevicePath : "";
        }

        private static bool GetAdvancedColorEnabled(DISPLAYCONFIG_PATH_TARGET_INFO target)
        {
            DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2 modern = new DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2();
            modern.header.type = 15;
            modern.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2));
            modern.header.adapterId = target.adapterId;
            modern.header.id = target.id;
            if (DisplayConfigGetAdvancedColorInfo2(ref modern) == ERROR_SUCCESS)
                return modern.activeColorMode == 2;
            DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO packet = new DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO();
            packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO;
            packet.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO));
            packet.header.adapterId = target.adapterId;
            packet.header.id = target.id;

            int rc = DisplayConfigGetAdvancedColorInfo(ref packet);
            if (rc != ERROR_SUCCESS)
                throw new InvalidOperationException("Advanced Color query failed: " + rc);

            return (packet.value & 0x2u) != 0;
        }

        public static DisplayInfo[] GetActiveDisplays()
        {
            for (int attempt = 0; attempt < 4; attempt++)
            {
                UInt32 pathCount;
                UInt32 modeCount;
                int rc = GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, out pathCount, out modeCount);
                if (rc != ERROR_SUCCESS)
                    throw new InvalidOperationException("GetDisplayConfigBufferSizes failed: " + rc);

                DISPLAYCONFIG_PATH_INFO[] paths = new DISPLAYCONFIG_PATH_INFO[pathCount];

                // We do not inspect mode information. Over-allocate each entry so Windows
                // can safely populate its native DISPLAYCONFIG_MODE_INFO array.
                long modeBytes = Math.Max(1L, (long)modeCount) * 256L;
                IntPtr modeBuffer = Marshal.AllocHGlobal(new IntPtr(modeBytes));

                try
                {
                    UInt32 pathCount2 = pathCount;
                    UInt32 modeCount2 = modeCount;

                    rc = QueryDisplayConfig(
                        QDC_ONLY_ACTIVE_PATHS,
                        ref pathCount2,
                        paths,
                        ref modeCount2,
                        modeBuffer,
                        IntPtr.Zero);

                    if (rc == ERROR_INSUFFICIENT_BUFFER)
                        continue;

                    if (rc != ERROR_SUCCESS)
                        throw new InvalidOperationException("QueryDisplayConfig failed: " + rc);

                    List<DisplayInfo> result = new List<DisplayInfo>();

                    for (int i = 0; i < pathCount2; i++)
                    {
                        DISPLAYCONFIG_PATH_INFO path = paths[i];
                        string gdiName = GetSourceName(path.sourceInfo);
                        if (String.IsNullOrWhiteSpace(gdiName))
                            continue;

                        DisplayInfo info = new DisplayInfo();
                        info.GdiName = gdiName;
                        info.DevicePath = GetDevicePath(path.targetInfo);
                        info.AdapterLow = path.targetInfo.adapterId.LowPart;
                        info.AdapterHigh = path.targetInfo.adapterId.HighPart;
                        info.SourceId = path.sourceInfo.id;
                        info.TargetId = path.targetInfo.id;
                        info.AdvancedColorEnabled = GetAdvancedColorEnabled(path.targetInfo);
                        info.Timing = path.targetInfo.refreshRate.Numerator + "/" + path.targetInfo.refreshRate.Denominator + ":" + path.targetInfo.rotation;
                        result.Add(info);
                    }

                    return result.ToArray();
                }
                finally
                {
                    Marshal.FreeHGlobal(modeBuffer);
                }
            }

            throw new InvalidOperationException("Display configuration kept changing while it was being queried.");
        }

        private static LUID ToLuid(DisplayInfo display)
        {
            LUID luid = new LUID();
            luid.LowPart = display.AdapterLow;
            luid.HighPart = display.AdapterHigh;
            return luid;
        }

        public static double GetSdrWhiteLevelNits(DisplayInfo display)
        {
            DISPLAYCONFIG_SDR_WHITE_LEVEL packet = new DISPLAYCONFIG_SDR_WHITE_LEVEL();
            packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL;
            packet.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_SDR_WHITE_LEVEL));
            packet.header.adapterId = display.AdapterLuid;
            packet.header.id = display.TargetId;
            int rc = DisplayConfigGetSdrWhiteLevel(ref packet);
            if (rc != ERROR_SUCCESS)
                return 200.0;
            return ((double)packet.SDRWhiteLevel / 1000.0) * 80.0;
        }

        public static int GetSelectedScope(DisplayInfo display)
        {
            int scope;
            int hr = ColorProfileGetDisplayUserScope(ToLuid(display), display.SourceId, out scope);
            if (hr < 0)
                return WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER;
            return scope;
        }

        public static string GetDefaultProfile(DisplayInfo display, int scope, int subtype)
        {
            IntPtr ptr = IntPtr.Zero;
            int hr = ColorProfileGetDisplayDefault(
                scope,
                ToLuid(display),
                display.SourceId,
                CPT_ICC,
                subtype,
                out ptr);

            try
            {
                if (hr < 0 || ptr == IntPtr.Zero) return null;
                return Marshal.PtrToStringUni(ptr);
            }
            finally
            {
                if (ptr != IntPtr.Zero) LocalFree(ptr);
            }
        }

        public static string GetDefaultProfileWithFallback(DisplayInfo display, int subtype)
        {
            int selected = GetSelectedScope(display);
            string value = GetDefaultProfile(display, selected, subtype);
            if (!String.IsNullOrWhiteSpace(value))
                return value;

            int other = selected == WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
                ? WCS_PROFILE_MANAGEMENT_SCOPE_SYSTEM_WIDE
                : WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER;

            return GetDefaultProfile(display, other, subtype);
        }

        [DllImport("mscms.dll", CharSet = CharSet.Unicode)]
        private static extern int ColorProfileAddDisplayAssociation(int scope, string profileName,
            LUID adapter, UInt32 sourceId, [MarshalAs(UnmanagedType.Bool)] bool makeDefault,
            [MarshalAs(UnmanagedType.Bool)] bool advanced);

        public static int SetCurrentUserDefault(DisplayInfo display, int subtype, string profileName)
        {
            if (String.IsNullOrWhiteSpace(profileName))
                return 0;

            string name = System.IO.Path.GetFileName(profileName);
            if (!System.IO.File.Exists(System.IO.Path.Combine(GetWindowsColorDirectory(), name)))
                return unchecked((int)0x80070002);
            int association = ColorProfileAddDisplayAssociation(
                WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER, name, ToLuid(display),
                display.SourceId, false, subtype == CPST_EXTENDED_DISPLAY_COLOR_MODE);
            if (association < 0) return association;
            return ColorProfileSetDisplayDefaultAssociation(
                WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
                profileName,
                CPT_ICC,
                subtype,
                ToLuid(display),
                display.SourceId);
        }
    }
}
'@

if (-not ('ColorProfileWatchdog.Native' -as [type])) {
    Add-Type -TypeDefinition $nativeSource -Language CSharp
}

function Save-JsonAtomic {
    param([string]$Path, $Value)
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $json = $Value | ConvertTo-Json -Depth 12
        [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Confirm-RestoredProfile {
    param($Display, [int]$Subtype, [string]$Expected)
    $actual = [ColorProfileWatchdog.Native]::GetDefaultProfile($Display,
        [ColorProfileWatchdog.Native]::WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER, $Subtype)
    if ([IO.Path]::GetFileName($actual) -ine [IO.Path]::GetFileName($Expected)) {
        Write-Log ('Profile readback mismatch on {0}: requested={1}; readback={2}' -f $Display.GdiName, $Expected, $actual)
    } else {
        Write-Log ('Verified profile association on {0}: {1}' -f $Display.GdiName, $actual)
    }
}

function Get-ActiveDisplays {
    return [ColorProfileWatchdog.Native]::GetActiveDisplays()
}

function Format-HResult {
    param([int]$Value)
    try {
        $bytes = [BitConverter]::GetBytes([int]$Value)
        $unsigned = [BitConverter]::ToUInt32($bytes, 0)
        return ('0x{0:X8}' -f $unsigned)
    } catch {
        return [string]$Value
    }
}

function Test-InstalledColorProfile {
    param([string]$ProfileName)
    if ([string]::IsNullOrWhiteSpace($ProfileName)) { return $false }
    try {
        $colorDir = [ColorProfileWatchdog.Native]::GetWindowsColorDirectory()
        return Test-Path -LiteralPath (Join-Path $colorDir $ProfileName)
    } catch {
        return $false
    }
}

function Resolve-StableWorkingPair {
    param(
        $Display,
        [string]$CurrentExtended,
        $GammaEntry
    )

    $off = $null
    $on = $null
    $enabled = $false

    # Highest-authority source: the profile that Windows is actually using now.
    # Stable Virtual HDR OSD working slots always use:
    #   Virtual_HDR_OSD_<display-token>_Off.icm
    #   Virtual_HDR_OSD_<display-token>_On.icm
    if ($CurrentExtended -match '^(Virtual_HDR_OSD_[A-Fa-f0-9]+)_(On|Off)\.icm$') {
        $prefix = $Matches[1]
        $slot = $Matches[2]
        $candidateOff = "${prefix}_Off.icm"
        $candidateOn = "${prefix}_On.icm"

        if (Test-InstalledColorProfile $candidateOff) { $off = $candidateOff }
        if (Test-InstalledColorProfile $candidateOn) { $on = $candidateOn }
        $enabled = ($slot -eq 'On')
    }

    # Runtime JSON is secondary only. Accept stable current-generation names and
    # verify that the files are actually installed before trusting them.
    if ($GammaEntry) {
        try {
            $candidate = [string]$GammaEntry.profiles.Off
            if (-not $off -and
                $candidate -match '^Virtual_HDR_OSD_[A-Fa-f0-9]+_Off\.icm$' -and
                (Test-InstalledColorProfile $candidate)) {
                $off = $candidate
            }
        } catch {}

        try {
            $candidate = [string]$GammaEntry.profiles.On
            if (-not $on -and
                $candidate -match '^Virtual_HDR_OSD_[A-Fa-f0-9]+_On\.icm$' -and
                (Test-InstalledColorProfile $candidate)) {
                $on = $candidate
            }
        } catch {}

        # Only use the JSON enable flag if the active Windows profile did not
        # already tell us which stable slot is selected.
        if ($CurrentExtended -notmatch '^Virtual_HDR_OSD_[A-Fa-f0-9]+_(On|Off)\.icm$') {
            try { $enabled = [bool]$GammaEntry.enabled } catch {}
        }
    }

    return [PSCustomObject]@{
        Off = $off
        On = $on
        Enabled = $enabled
    }
}

function Get-SavedProfileState {
    param($Display)

    $scope = [ColorProfileWatchdog.Native]::GetSelectedScope($Display)
    $standard = [ColorProfileWatchdog.Native]::GetDefaultProfileWithFallback(
        $Display,
        [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE
    )
    $extended = [ColorProfileWatchdog.Native]::GetDefaultProfileWithFallback(
        $Display,
        [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE
    )

    $gammaEntry = Get-GammaEntryForDisplay -CurrentDisplay $Display
    $pair = Resolve-StableWorkingPair -Display $Display -CurrentExtended $extended -GammaEntry $gammaEntry
    if ($gammaEntry -and $gammaEntry.profiles.Off -and $gammaEntry.profiles.On -and
        $gammaEntry.profiles.Off -match '^Virtual_HDR_OSD_[A-Fa-f0-9]+_Off\.icm$' -and
        $gammaEntry.profiles.On -match '^Virtual_HDR_OSD_[A-Fa-f0-9]+_On\.icm$' -and
        (Test-InstalledColorProfile $gammaEntry.profiles.Off) -and
        (Test-InstalledColorProfile $gammaEntry.profiles.On)) {
        $pair.Off = [string]$gammaEntry.profiles.Off
        $pair.On = [string]$gammaEntry.profiles.On
        $pair.Enabled = [bool]$gammaEntry.enabled
    }

    [PSCustomObject]@{
        GdiName         = $Display.GdiName
        DevicePath      = $Display.DevicePath
        AdapterLow      = $Display.AdapterLow
        AdapterHigh     = $Display.AdapterHigh
        SourceId        = $Display.SourceId
        TargetId        = $Display.TargetId
        OriginalScope   = $scope
        StandardProfile = $standard
        ExtendedProfile = (Resolve-BaseExtendedProfile -GammaEntry $gammaEntry -CurrentExtended $extended)
        WorkingOff      = $pair.Off
        WorkingOn       = $pair.On
        GammaEnabled    = [bool]$pair.Enabled
        GammaUpdatedAt  = (Get-Date).ToString('o')
    }
}

function Get-OptionalField {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

function Find-SavedDisplay {
    param($CurrentDisplay, $State = $script:state)
    foreach ($saved in $State.Displays) {
        $savedPath = Get-OptionalField $saved DevicePath
        $currentPath = Get-OptionalField $CurrentDisplay DevicePath
        if ($savedPath -and $currentPath) {
            if ($savedPath -ieq $currentPath) { return $saved }
            continue
        }
        if ($saved.GdiName -eq $CurrentDisplay.GdiName -and
            $saved.AdapterLow -eq $CurrentDisplay.AdapterLow -and
            $saved.AdapterHigh -eq $CurrentDisplay.AdapterHigh -and
            $saved.SourceId -eq $CurrentDisplay.SourceId -and
            $saved.TargetId -eq $CurrentDisplay.TargetId) { return $saved }
    }
    return $null
}

function Get-GammaEntryForDisplay {
    param($CurrentDisplay)

    if (-not (Test-Path -LiteralPath $GammaStatePath)) { return $null }
    try {
        # Cache parsed JSON by write time and length. Native file reads avoid idle
        # Get-Content pipelines; changing records replace the single cached object.
        $stampNow = $null
        try {
            $gammaInfo = New-Object IO.FileInfo($GammaStatePath)
            $stampNow = '{0}:{1}' -f $gammaInfo.LastWriteTimeUtc.Ticks, $gammaInfo.Length
        } catch {}

        $gamma = $null
        if ($null -ne $stampNow -and $stampNow -eq $script:GammaCacheStamp) {
            # Unchanged since the last read. Fall through to the selection below with
            # the parse already in hand, so the cache changes only how $gamma is
            # obtained and never which record is chosen.
            $gamma = $script:GammaCacheValue
        }
        # The GUI publishes this file by writing a temporary copy and renaming over
        # the original, so a read landing in that window fails. Returning $null then
        # makes the caller fall back to the state captured at install time, which can
        # assert the OPPOSITE correction variant -- the user's choice appears to
        # revert seconds after they make it. Retry briefly, and log once if every
        # attempt fails.
        $readError = $null
        for ($attempt = 1; $attempt -le 4 -and -not $gamma; $attempt++) {
            try {
                # [IO.File]::ReadAllText rather than Get-Content -Raw: same bytes, same
                # BOM handling, 96 bytes per call retained against 144,821. Measured.
                $raw = [System.IO.File]::ReadAllText($GammaStatePath)
                if (-not [string]::IsNullOrWhiteSpace($raw)) {
                    $gamma = $raw | ConvertFrom-Json
                    break
                }
            } catch { $readError = $_.Exception.Message }
            Start-Sleep -Milliseconds (40 * $attempt)
        }
        if ($gamma -and $null -ne $stampNow) {
            $script:GammaCacheStamp = $stampNow
            $script:GammaCacheValue = $gamma
        }
        if ($gamma) {
            Clear-LogOnce 'GAMMA-READ'
        } elseif ($readError) {
            Write-LogOnce 'GAMMA-READ' ('Could not read gamma_hotkeys.json after four attempts ({0}); using the state captured at install.' -f $readError)
        }
        if (-not $gamma) { return $null }
        if (-not $gamma.displays) { return $null }

        # Records are keyed by an id derived from the adapter LUID, which Windows
        # reissues on reboots and driver restarts, so one monitor accumulates several
        # records carrying the same gdi_name. Returning the first match handed the
        # caller a stale record naming profiles that no longer exist, defeating the
        # intent comparison in Get-DesiredExtendedProfile. Take the newest record.
        # Match on the monitor's EDID device path whenever both sides carry one, the
        # way Find-SavedDisplay already does for State.json. gdi_name is \\.\DISPLAY1 --
        # a slot Windows reassigns between sessions -- so after a hotplug this handed one
        # monitor the profile pair belonging to another, while the State.json lookup
        # beside it, already fixed, disagreed. Records written before the GUI published a
        # path have none, and still match by slot.
        $wantPath = [string](Get-OptionalField $CurrentDisplay DevicePath)
        $candidates = @()
        foreach ($property in $gamma.displays.PSObject.Properties) {
            $entryPath = [string](Get-OptionalField $property.Value device_path)
            if ($wantPath -and $entryPath) {
                if ($entryPath -eq $wantPath) { $candidates += $property.Value }
            } elseif ($property.Value.gdi_name -eq $CurrentDisplay.GdiName) {
                $candidates += $property.Value
            }
        }
        if ($candidates.Count -eq 0) { return $null }
        if ($candidates.Count -eq 1) { return $candidates[0] }

        # When no timestamp is usable, fall back to the last record written.
        $best = $candidates[$candidates.Count - 1]
        $bestAt = $null
        foreach ($candidate in $candidates) {
            $at = ConvertTo-GammaTimestamp ([string]$candidate.updated_at)
            if ($null -eq $at) { continue }
            if (($null -eq $bestAt) -or ($at -gt $bestAt)) {
                $best = $candidate
                $bestAt = $at
            }
        }
        return $best
    } catch {}
    return $null
}

$script:LastLogOnce = @{}
$script:GammaCacheStamp = $null
$script:GammaCacheValue = $null

function Write-LogOnce {
    param([string]$Key, [string]$Message)

    # For conditions that are a *state* rather than an event. The four call sites --
    # a requested correction whose profiles are not installed, a failing STANDARD or
    # EXTENDED write, and gamma_hotkeys.json that cannot be read -- all sit on the
    # reconcile path, which runs about 1.3 times a
    # second per display. At roughly 140 bytes a line that is 500 KB in an hour, and
    # Write-Log rotates at 512 KB keeping one .old, so a fault that lasted two hours
    # erased every line from before it happened. The log exists to answer "what took my
    # profile, and when"; flooding it with the consequence destroys the evidence of the
    # cause.
    #
    # Keyed on display and site, deduped on the message itself rather than on a timer,
    # so a fault that changes -- a different HRESULT, a different variant -- is reported
    # again immediately. Clear-LogOnce is called from the matching success branch, so a
    # condition that comes back is logged afresh rather than silenced forever.
    if ($script:LastLogOnce[$Key] -eq $Message) { return }
    if ($script:LastLogOnce.Count -ge 128 -and -not $script:LastLogOnce.ContainsKey($Key)) { $script:LastLogOnce.Clear() }
    $script:LastLogOnce[$Key] = $Message
    Write-Log $Message
}

function Clear-LogOnce {
    param([string]$Key)

    if ($script:LastLogOnce.ContainsKey($Key)) { $script:LastLogOnce.Remove($Key) }
}

function ConvertTo-GammaTimestamp {
    param([string]$Value)

    # Both sides have written slightly different ISO-8601 shapes: the GUI wrote a naive
    # local time for a long while, this script writes a round-trip value with an offset.
    # AssumeLocal makes the naive form comparable. A malformed or empty value must return
    # $null rather than throw, because $ErrorActionPreference is 'Stop' here.
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $parsed = [datetimeoffset]::MinValue
    if ([datetimeoffset]::TryParse(
            $Value,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AssumeLocal,
            [ref]$parsed)) {
        return $parsed
    }
    return $null
}

function Resolve-BaseExtendedProfile {
    param($GammaEntry, [string]$CurrentExtended)

    # The captured HDR fallback must be the user's own profile, never one of Virtual
    # HDR OSD's generated working profiles: adopting one makes the watchdog restore
    # already-edited data as though it were the source.
    #
    # The GUI publishes both a name and a full path. Older builds wrote the ICC
    # description into the name field, which is not a filename at all -- Windows HDR
    # Calibration describes a profile with slashes in it -- so the path is tried as
    # well before giving up.
    $candidates = @()
    if ($GammaEntry) {
        if ($GammaEntry.PSObject.Properties.Name -contains 'base_profile') {
            $candidates += [string]$GammaEntry.base_profile
        }
        if ($GammaEntry.PSObject.Properties.Name -contains 'base_profile_path') {
            $raw = [string]$GammaEntry.base_profile_path
            if (-not [string]::IsNullOrWhiteSpace($raw)) {
                try { $candidates += [System.IO.Path]::GetFileName($raw) } catch {}
            }
        }
    }
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        if ($candidate -match '^Virtual_HDR_OSD_') { continue }
        if (Test-InstalledColorProfile $candidate) { return $candidate }
    }

    # Nothing usable. Keep the current association only when it is not ours; an
    # app-managed name here would be a circular fallback, so report none instead.
    if ($CurrentExtended -match '^Virtual_HDR_OSD_') { return '' }
    return $CurrentExtended
}

function Get-DesiredExtendedProfile {
    param($CurrentDisplay, $SavedDisplay, [switch]$Persist)

    $entry = Get-GammaEntryForDisplay -CurrentDisplay $CurrentDisplay

    # Whichever side acted most recently wins.
    #
    # Virtual HDR OSD records its own correction changes in gamma_hotkeys.json; this
    # script records the switches it performs itself in State.json. Previously only the
    # captured GammaEnabled below was consulted, so a correction change made in the GUI
    # was re-associated back to the opposite variant by the next forced restore, roughly
    # every five seconds, with no way for the user to make it stick.
    if ($entry -and ($entry.PSObject.Properties.Name -contains 'enabled')) {
        $guiAt = ConvertTo-GammaTimestamp ([string]$entry.updated_at)
        $ownAt = $null
        if ($SavedDisplay.PSObject.Properties.Name -contains 'GammaUpdatedAt') {
            $ownAt = ConvertTo-GammaTimestamp ([string]$SavedDisplay.GammaUpdatedAt)
        }

        if ($guiAt -and ((-not $ownAt) -or ($guiAt -gt $ownAt))) {
            $wanted = $(if ([bool]$entry.enabled) { 'On' } else { 'Off' })

            # Prefer the filenames the GUI last published. It regenerates the working pair
            # under new names whenever the adapter LUID changes, which State.json captured
            # at install time cannot know about.
            $name = $null
            if (($entry.PSObject.Properties.Name -contains 'profiles') -and $entry.profiles) {
                if ($entry.profiles.PSObject.Properties.Name -contains $wanted) {
                    $name = [string]$entry.profiles.$wanted
                }
            }
            if ([string]::IsNullOrWhiteSpace($name)) {
                $name = [string]$(if ($wanted -eq 'On') { $SavedDisplay.WorkingOn } else { $SavedDisplay.WorkingOff })
            }

            # Never hand Windows a profile that is not installed.
            if ((-not [string]::IsNullOrWhiteSpace($name)) -and (Test-InstalledColorProfile -ProfileName $name)) {
                # Recovered: forget the "not installed" line so the next occurrence is
                # reported rather than deduped against a fault that has since been fixed.
                Clear-LogOnce ('{0}|GAMMA-UNAVAILABLE' -f $CurrentDisplay.GdiName)
                if ($Persist -and $SavedDisplay.GammaUpdatedAt -ne [string]$entry.updated_at) {
                    $SavedDisplay.GammaEnabled = [bool]$entry.enabled
                    $SavedDisplay.GammaUpdatedAt = [string]$entry.updated_at
                    if ($entry.profiles.Off -and $entry.profiles.On -and
                        (Test-InstalledColorProfile $entry.profiles.Off) -and
                        (Test-InstalledColorProfile $entry.profiles.On)) {
                        $SavedDisplay.WorkingOff = [string]$entry.profiles.Off
                        $SavedDisplay.WorkingOn = [string]$entry.profiles.On
                    }
                    Save-JsonAtomic -Path $StatePath -Value $state
                }
                return $name
            }

            # The requested variant is unusable. Falling through to the captured state
            # here would answer with the OPPOSITE variant whenever GammaEnabled still
            # disagrees -- turning the correction back ON moments after the user chose
            # Off, which is precisely the guarantee this block exists to keep. Try the
            # captured name for the SAME direction, and otherwise change nothing.
            $sameDirection = [string]$(if ($wanted -eq 'On') { $SavedDisplay.WorkingOn } else { $SavedDisplay.WorkingOff })
            if ((-not [string]::IsNullOrWhiteSpace($sameDirection)) -and
                (Test-InstalledColorProfile -ProfileName $sameDirection)) {
                Clear-LogOnce ('{0}|GAMMA-UNAVAILABLE' -f $CurrentDisplay.GdiName)
                return $sameDirection
            }
            # Once per distinct message: this branch's precondition -- the GUI asking for
            # a variant whose profile is not installed -- is a state that persists until
            # someone reinstalls it, and it is reached on every reconcile pass.
            Write-LogOnce ('{0}|GAMMA-UNAVAILABLE' -f $CurrentDisplay.GdiName) (
                'Gamma correction {0} was requested for {1} but no installed profile provides it; leaving the association unchanged.' -f $wanted.ToUpper(), $CurrentDisplay.GdiName)
            return ''
        }
    }

    # The standalone watchdog owns a persistent copy of the prepared Off/On names.
    # This works even when Virtual HDR OSD is closed and its runtime JSON is stale.
    #
    # "Never hand Windows a profile that is not installed" applied only to the branch
    # above, and these names need it more, not less: they are captured at install time
    # and the working pair embeds the adapter LUID, which Windows reissues across
    # reboots, so they go stale by design rather than by accident.
    #
    # A missing name here returns '' -- change nothing -- rather than falling through to
    # the next candidate. Falling through would answer with the OPPOSITE variant
    # whenever the pair is half-installed, turning the correction back on moments after
    # the user chose Off, which is the same guarantee the block above exists to keep.
    if ($SavedDisplay.PSObject.Properties.Name -contains 'GammaEnabled') {
        if ([bool]$SavedDisplay.GammaEnabled -and $SavedDisplay.WorkingOn) {
            if (Test-InstalledColorProfile -ProfileName ([string]$SavedDisplay.WorkingOn)) {
                return [string]$SavedDisplay.WorkingOn
            }
            return ''
        }
        if (-not [bool]$SavedDisplay.GammaEnabled -and $SavedDisplay.WorkingOff) {
            if (Test-InstalledColorProfile -ProfileName ([string]$SavedDisplay.WorkingOff)) {
                return [string]$SavedDisplay.WorkingOff
            }
            return ''
        }
    }

    # No pair captured for this display, so there is no variant to get wrong and the
    # remaining candidates can be tried in turn.
    if ($entry -and $entry.active_profile -and
        (Test-InstalledColorProfile -ProfileName ([string]$entry.active_profile))) {
        return [string]$entry.active_profile
    }
    if ($SavedDisplay.ExtendedProfile -and
        (Test-InstalledColorProfile -ProfileName ([string]$SavedDisplay.ExtendedProfile))) {
        return [string]$SavedDisplay.ExtendedProfile
    }
    return ''
}

function Test-RecoveryHeld {
    param($Entry)
    if (-not $Entry -or -not $Entry.recovery_hold_until) { return $false }
    try { return [DateTime]::Parse([string]$Entry.recovery_hold_until).ToUniversalTime() -gt [DateTime]::UtcNow }
    catch { return $false }
}

function Restore-SavedProfiles {
    param(
        $CurrentDisplay,
        $SavedDisplay,
        [switch]$Force
    )

    $runtime = Get-GammaEntryForDisplay -CurrentDisplay $CurrentDisplay
    if ($runtime -and $runtime.restored) { return }
    if (Test-RecoveryHeld $runtime) {
        [ColorProfileWatchdog.Native]::PauseRecoveryFor(3000)
        return
    }
    $standard = [string]$SavedDisplay.StandardProfile
    if ($runtime -and $runtime.sdr_unmanaged) { $standard = '' }
    elseif ($runtime -and $runtime.sdr_profile -and (Test-InstalledColorProfile $runtime.sdr_profile)) {
        $standard = [string]$runtime.sdr_profile
    }
    if (-not $CurrentDisplay.AdvancedColorEnabled -and $standard) {
        $current = [ColorProfileWatchdog.Native]::GetDefaultProfile(
            $CurrentDisplay,
            [ColorProfileWatchdog.Native]::WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE
        )

        if ($Force -or $current -ne $standard) {
            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $CurrentDisplay,
                [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE,
                [string]$standard
            )
            if ($hr -lt 0) {
                Write-LogOnce ('{0}|STANDARD' -f $CurrentDisplay.GdiName) ('Failed to restore STANDARD profile on {0}: HRESULT {1}' -f $CurrentDisplay.GdiName, (Format-HResult $hr))
            } else {
                Clear-LogOnce ('{0}|STANDARD' -f $CurrentDisplay.GdiName)
                Confirm-RestoredProfile -Display $CurrentDisplay -Subtype ([ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE) -Expected $standard
            }
        }
    }

    $desiredExtended = Get-DesiredExtendedProfile -CurrentDisplay $CurrentDisplay -SavedDisplay $SavedDisplay -Persist
    if ($CurrentDisplay.AdvancedColorEnabled -and $desiredExtended) {
        $current = [ColorProfileWatchdog.Native]::GetDefaultProfile(
            $CurrentDisplay,
            [ColorProfileWatchdog.Native]::WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE
        )

        if ($Force -or $current -ne $desiredExtended) {
            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $CurrentDisplay,
                [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE,
                [string]$desiredExtended
            )
            if ($hr -lt 0) {
                Write-LogOnce ('{0}|EXTENDED' -f $CurrentDisplay.GdiName) ('Failed to restore EXTENDED profile on {0}: HRESULT {1}' -f $CurrentDisplay.GdiName, (Format-HResult $hr))
            } else {
                Clear-LogOnce ('{0}|EXTENDED' -f $CurrentDisplay.GdiName)
                Confirm-RestoredProfile -Display $CurrentDisplay -Subtype ([ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE) -Expected $desiredExtended
            }
        }
    }
}

function Invoke-GammaHotkey {
    param([bool]$Enable)

    try {
        $currentDisplays = @(Get-ActiveDisplays)
        foreach ($current in $currentDisplays) {
            $saved = Find-SavedDisplay -CurrentDisplay $current
            if (-not $saved) { continue }
            $runtime = Get-GammaEntryForDisplay -CurrentDisplay $current
            if (($runtime -and $runtime.restored) -or (Test-RecoveryHeld $runtime)) {
                Write-Log ('Gamma hotkey ignored on {0}: Windows profile is restored or calibration owns the display.' -f $current.GdiName)
                continue
            }

            $profile = $(if ($Enable) { $saved.WorkingOn } else { $saved.WorkingOff })
            if (-not $profile) {
                # Backward-compatible fallback if this watchdog was installed before the
                # self-contained state fields existed.
                $entry = Get-GammaEntryForDisplay -CurrentDisplay $current
                if ($entry) {
                    $profile = $(if ($Enable) { $entry.profiles.On } else { $entry.profiles.Off })
                }
            }
            if (-not $profile) {
                Write-Log ('Gamma hotkey ignored on {0}: stable {1} profile was not captured. Re-run the installer after Virtual HDR OSD has prepared both working profiles.' -f $current.GdiName, $(if($Enable){'ON'}else{'OFF'}))
                continue
            }

            $saved.GammaEnabled = [bool]$Enable
            $saved.GammaUpdatedAt = (Get-Date).ToString("o")
            Save-JsonAtomic -Path $StatePath -Value $state

            # Keep the GUI runtime file synchronized when it exists, but do not depend on it.
            try {
                if (Test-Path -LiteralPath $GammaStatePath) {
                    $gamma = ([IO.File]::ReadAllText($GammaStatePath)) | ConvertFrom-Json
                    foreach ($property in $gamma.displays.PSObject.Properties) {
                        if (($current.DevicePath -and $property.Value.device_path -ieq $current.DevicePath) -or
                            (-not $property.Value.device_path -and $property.Value.gdi_name -eq $current.GdiName)) {
                            $property.Value.enabled = [bool]$Enable
                            $property.Value.active_profile = [string]$profile
                            $property.Value.updated_at = $saved.GammaUpdatedAt
                        }
                    }
                    Save-JsonAtomic -Path $GammaStatePath -Value $gamma
                }
            } catch {}

            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $current,
                [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE,
                [string]$profile
            )
            if ($hr -lt 0) {
                [ColorProfileWatchdog.Native]::RequestRecovery()
                Write-Log ('Gamma hotkey profile switch failed on {0}: HRESULT {1}' -f $current.GdiName, (Format-HResult $hr))
                continue
            }

            $verify = [ColorProfileWatchdog.Native]::GetDefaultProfileWithFallback(
                $current,
                [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE
            )
            Write-Log ('Gamma correction {0} on {1}: requested={2}; readback={3}' -f $(if($Enable){'ON'}else{'OFF'}), $current.GdiName, $profile, $verify)
        }
    } catch {
        Write-Log ('Gamma hotkey error: ' + $_.Exception.Message)
    }
}

function Invoke-ManualRestore {
    param([switch]$Automatic)
    # No sleep/retry loop here: the native bounded schedule owns retries.
    if (-not $Automatic) { [ColorProfileWatchdog.Native]::RequestRecovery() }
    try {
        $displays = [ColorProfileWatchdog.Native]::GetActiveDisplays()
        foreach ($current in $displays) {
            $saved = Find-SavedDisplay -CurrentDisplay $current
            if ($saved) { Restore-SavedProfiles -CurrentDisplay $current -SavedDisplay $saved -Force }
        }
    } catch { Write-Log ('Recovery query failed; later attempts remain available: ' + $_.Exception.Message) }
}

if ($Install) {
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Remove-Item -LiteralPath $ResultPath -Force -ErrorAction SilentlyContinue
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 20348) {
        throw 'This standalone watchdog requires Windows build 20348 or newer.'
    }

    $displays = @(Get-ActiveDisplays)
    if ($displays.Count -eq 0) {
        throw 'No active displays were found.'
    }

    $saved = @()
    foreach ($display in $displays) {
        $item = Get-SavedProfileState -Display $display
        # If the GUI has recorded the real HDR base profile, preserve that as the
        # watchdog fallback instead of capturing an app-managed working profile.
        $gammaEntry = Get-GammaEntryForDisplay -CurrentDisplay $display
        if ($gammaEntry -and $gammaEntry.base_profile) {
            $baseCandidate = [string]$gammaEntry.base_profile
            if ($baseCandidate -and
                $baseCandidate -notmatch '^Virtual_HDR_OSD_' -and
                (Test-InstalledColorProfile $baseCandidate)) {
                $item.ExtendedProfile = $baseCandidate
            }
        }
        $saved += $item
    }

    foreach ($item in $saved) {
        $hasAnyWorking = [bool]$item.WorkingOff -or [bool]$item.WorkingOn
        if ($hasAnyWorking -and (-not $item.WorkingOff -or -not $item.WorkingOn)) {
            throw ('Virtual HDR OSD working pair is incomplete on {0}. Open Virtual HDR OSD, apply the current HDR state once so both Correction Off and Correction On exist, close the GUI, then run this installer again.' -f $item.GdiName)
        }
    }

    Stop-ExistingWatchdog
    # Re-read after stopping the old reconciler; it cannot overwrite GUI intent
    # between the final capture and startup of the new instance.
    $saved = @()
    foreach ($display in $displays) { $saved += Get-SavedProfileState -Display $display }
    $state = [PSCustomObject]@{
        Version     = 4
        CapturedAt  = (Get-Date).ToString('o')
        Displays    = $saved
    }

    Save-JsonAtomic -Path $StatePath -Value $state

    $escapedPs1 = $PSCommandPath.Replace('"', '""')
    $vbs = @"
Set shell = CreateObject("WScript.Shell")
If WScript.Arguments.Named.Exists("logon") Then WScript.Sleep 10000
failures = 0
Do
  started = Timer
  code = shell.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$escapedPs1""", 0, True)
  If code = 0 Or code = 2 Or code = 3 Or code = 4 Then
    WScript.Quit 0
  End If
  elapsed = Timer - started
  If elapsed < 0 Then elapsed = elapsed + 86400
  If elapsed >= 60 Then failures = 0
  failures = failures + 1
  If failures >= 3 Then WScript.Quit 1
  WScript.Sleep 5000
Loop
"@
    Set-Content -LiteralPath $LauncherPath -Value $vbs -Encoding Unicode

    # Register persistence through the Task Scheduler COM API instead of the
    # ScheduledTasks CIM cmdlets.  A SID is unambiguous for local, Microsoft,
    # Entra ID, and domain-backed interactive accounts, and InteractiveToken
    # requires no stored password. HKCU fallback uses the same bounded launcher
    # and native worker; only registration differs, never the recovery loop.
    $startupMethod = 'Task Scheduler (COM / current-user SID)'
    $startupWarnings = @()
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $currentSid = $currentIdentity.User.Value
    $wscriptPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $taskArguments = '//B //Nologo "{0}"' -f $LauncherPath

    $taskService = $null
    $taskRoot = $null
    $taskDefinition = $null
    $logonTrigger = $null
    $execAction = $null
    try {
        $taskService = New-Object -ComObject 'Schedule.Service'
        $taskService.Connect()
        $taskRoot = $taskService.GetFolder('\')

        # Delete a previous definition first. This also repairs tasks created by
        # older builds with an incompatible principal/trigger identity.
        try { $taskRoot.DeleteTask($TaskName, 0) } catch {}

        $taskDefinition = $taskService.NewTask(0)
        $taskDefinition.RegistrationInfo.Description = 'Keeps Windows SDR/HDR color profile associations stable across display-mode changes.'
        $taskDefinition.Settings.Enabled = $true
        $taskDefinition.Settings.StartWhenAvailable = $true
        $taskDefinition.Settings.DisallowStartIfOnBatteries = $false
        $taskDefinition.Settings.StopIfGoingOnBatteries = $false
        $taskDefinition.Settings.ExecutionTimeLimit = 'PT0S'
        $taskDefinition.Settings.MultipleInstances = 2  # TASK_INSTANCES_IGNORE_NEW

        # TASK_LOGON_INTERACTIVE_TOKEN = 3, TASK_RUNLEVEL_LUA = 0.
        $taskDefinition.Principal.UserId = $currentSid
        $taskDefinition.Principal.LogonType = 3
        $taskDefinition.Principal.RunLevel = 0

        # TASK_TRIGGER_LOGON = 9. LogonTrigger.UserId accepts a SID.
        $logonTrigger = $taskDefinition.Triggers.Create(9)
        $logonTrigger.UserId = $currentSid
        $logonTrigger.Delay = 'PT10S'
        $logonTrigger.Enabled = $true

        # TASK_ACTION_EXEC = 0.
        $execAction = $taskDefinition.Actions.Create(0)
        $execAction.Path = $wscriptPath
        $execAction.Arguments = $taskArguments

        # TASK_CREATE_OR_UPDATE = 6; TASK_LOGON_INTERACTIVE_TOKEN = 3.
        $taskRoot.RegisterTaskDefinition(
            $TaskName,
            $taskDefinition,
            6,
            $currentSid,
            $null,
            3,
            $null
        ) | Out-Null

        # Remove an old fallback entry if a previous installation needed it.
        Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Force -ErrorAction SilentlyContinue
    }
    catch {
        $registrationError = $_.Exception.Message
        $runCommand = '"{0}" //B //Nologo "{1}" /logon' -f $wscriptPath, $LauncherPath
        # Windows Run commands have a documented 260-character limit.
        if ($runCommand.Length -gt 260) {
            throw ('Task Scheduler failed and the HKCU startup command exceeds 260 characters: ' + $registrationError)
        }
        try {
            New-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Force -ErrorAction Stop | Out-Null
            New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Value $runCommand -PropertyType String -Force -ErrorAction Stop | Out-Null
        }
        catch { throw ('Task Scheduler and HKCU fallback registration both failed: ' + $_.Exception.Message) }
        $startupMethod = 'HKCU Run fallback (shared hidden launcher)'
        $startupWarnings = @('Task Scheduler registration failed (' + $registrationError + '). Using the current-user HKCU Run fallback.')
        Write-Warning $startupWarnings[0]
    }
    finally {
        if ($taskRoot) { [Runtime.InteropServices.Marshal]::ReleaseComObject($taskRoot) | Out-Null }
        if ($taskService) { [Runtime.InteropServices.Marshal]::ReleaseComObject($taskService) | Out-Null }
        $taskDefinition = $null
        $logonTrigger = $null
        $execAction = $null
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
    }

    Write-Host ''
    Write-Host 'Captured display profile associations:' -ForegroundColor Cyan
    foreach ($item in $saved) {
        Write-Host ('  {0}' -f $item.GdiName)
        Write-Host ('    SDR / STANDARD : {0}' -f $(if ($item.StandardProfile) { $item.StandardProfile } else { '<none - left untouched>' }))
        Write-Host ('    HDR / EXTENDED : {0}' -f $(if ($item.ExtendedProfile) { $item.ExtendedProfile } else { '<none - left untouched>' }))
        Write-Host ('    Gamma OFF      : {0}' -f $(if ($item.WorkingOff) { $item.WorkingOff } else { '<not prepared by Virtual HDR OSD>' }))
        Write-Host ('    Gamma ON       : {0}' -f $(if ($item.WorkingOn) { $item.WorkingOn } else { '<not prepared by Virtual HDR OSD>' }))
    }

    Write-Host ''
    Write-Host ('Startup mode: {0} / hidden / 10-second logon delay' -f $startupMethod) -ForegroundColor Green
    Write-Host ('State: {0}' -f $StatePath)
    Write-Host ('Log  : {0}' -f $LogPath)

    Start-Process -FilePath (Join-Path $env:WINDIR 'System32\wscript.exe') `
        -ArgumentList @('//B', '//Nologo', ('"{0}"' -f $LauncherPath)) `
        -WindowStyle Hidden
    $result = [PSCustomObject]@{
        ok = $true
        warnings = $startupWarnings
        startup  = $startupMethod
        recorded_at = [DateTime]::UtcNow.ToString('o')
    }
    Save-JsonAtomic -Path $ResultPath -Value $result

    exit 0
}

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\ColorProfileModeWatchdogStandalone', [ref]$createdNew)
if (-not $createdNew) {
    exit 0
}

try {
    [ColorProfileWatchdog.Native]::BeginStartupGuard($LogPath)
    Write-Log 'Startup: reading saved state.'
    if (-not (Test-Path -LiteralPath $StatePath)) {
        Write-Log 'State.json is missing; watchdog stopped.'
        exit 2
    }

    try { $state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json }
    catch { Write-Log ('State.json could not be parsed: ' + $_.Exception.Message); exit 2 }
    if (-not $state.Displays) {
        Write-Log 'No saved display associations; watchdog stopped.'
        exit 3
    }

    foreach ($savedDisplay in $state.Displays) {
        if ($null -eq $savedDisplay.TargetId -or -not $savedDisplay.GammaUpdatedAt) {
            Write-Log 'Legacy state: rerun the updated installer to capture display identities.'
            exit 4
        }
    }
    Write-Log 'Watchdog started.'

    $hotkeysRegistered = [ColorProfileWatchdog.Native]::TryRegisterGammaHotkeys()
    Write-Log $(if ($hotkeysRegistered) { 'Global hotkey thread active: Alt+1 OFF, Alt+2 ON, Alt+3 restore.' } else { 'Some hotkeys unavailable; free hotkeys remain active and missing registrations will be retried.' })
    $startupCompleted = [ColorProfileWatchdog.Native]::HotkeyLoopReady
    if ($startupCompleted) { [ColorProfileWatchdog.Native]::CompleteStartup(); Write-Log 'Startup completed.' }
    $maintenance = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        # Native code probes once per second and owns bounded retry deadlines.
        # PowerShell does no display pipelines, dates or logging while idle.
        $recover = [ColorProfileWatchdog.Native]::WaitForDisplayChange(250)
        $hotkeysRegistered = [ColorProfileWatchdog.Native]::TryRegisterGammaHotkeys()
        if (-not $startupCompleted -and [ColorProfileWatchdog.Native]::HotkeyLoopReady) {
            $startupCompleted = $true
            [ColorProfileWatchdog.Native]::CompleteStartup()
            Write-Log 'Startup completed after hotkey retry.'
        }
        for ($command = 0; $command -lt 16; $command++) {
            $hotkey = [ColorProfileWatchdog.Native]::PollGammaHotkey()
            if ($hotkey -eq 0) { break }
            try {
                if ($hotkey -eq [ColorProfileWatchdog.Native]::HOTKEY_OFF) { Invoke-GammaHotkey -Enable $false }
                elseif ($hotkey -eq [ColorProfileWatchdog.Native]::HOTKEY_ON) { Invoke-GammaHotkey -Enable $true }
                elseif ($hotkey -eq [ColorProfileWatchdog.Native]::HOTKEY_FORCE_RESTORE) { Invoke-ManualRestore }
            } catch { Write-Log ('Hotkey error: ' + $_.Exception.Message) }
        }
        if ($recover) { Invoke-ManualRestore -Automatic }
        # A bounded maintenance cadence rather than full blocking GC per event.
        if ($maintenance.ElapsedMilliseconds -ge 60000) {
            [GC]::Collect(0)
            $maintenance.Restart()
        }
    }

}
finally {
    [ColorProfileWatchdog.Native]::CompleteStartup()
    try { [ColorProfileWatchdog.Native]::UnregisterGammaHotkeys() } catch {}
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
}
