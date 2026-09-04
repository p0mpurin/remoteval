#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_agent.py - the CLEAN PC's remote-control agent for the 2PC method.
Runs on the clean PC (the vanilla machine). Exposes an enhanced HTTP API the
main-PC dashboard (wuw) polls:
    GET  /status            -> {"game": bool, "window": {...}, "state": "...", "map": "...", ...}
    GET  /window/focus      -> restore & bring Valorant window to front
    GET  /launch            -> start Riot Client + Valorant (--launch-product)
    GET  /kill              -> kill the VALORANT game + Riot Client
    GET  /log?n=50          -> last n state-relevant log lines
    GET  /queue?mode=...    -> start matchmaking (game's own API)
    GET  /cancel            -> cancel matchmaking
    GET  /pick?agent=Jett   -> agent select + lock when in pregame
    GET  /session           -> RSO/entitlements/party/session info

Uses standard library + built-in ctypes for native Win32 window inspection.
No external pip packages required. Run:
    py clean_agent.py          (listens on 0.0.0.0:8090)
"""

import http.server, json, os, subprocess, threading, time, re, ssl, base64, urllib.request, ctypes
from ctypes import wintypes

PORT = 8090
GAME_LOG = os.path.expandvars(r"%LOCALAPPDATA%\VALORANT\Saved\Logs\ShooterGame.log")
GAME_EXE = "VALORANT-Win64-Shipping.exe"
GAME_EXE_PATHS = [
    r"C:\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe",
    r"C:\Program Files\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe"),
]
LOCKFILE = os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\Config\lockfile")

# ── WIN32 WINDOW INSPECTION (NATIVE CTYPES) ──────────────────────────
user32 = ctypes.windll.user32

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long)
    ]

def get_valorant_window():
    """Find the Valorant window handle (hwnd) and title using Win32 API."""
    hwnd = user32.FindWindowW("UnrealWindow", None)
    if hwnd:
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if "VALORANT" in buf.value:
            return hwnd, buf.value

    val_hwnd = None
    val_title = ""
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def enum_cb(hwnd_cand, lparam):
        nonlocal val_hwnd, val_title
        if not user32.IsWindowVisible(hwnd_cand):
            return True
        length = user32.GetWindowTextLengthW(hwnd_cand)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd_cand, buf, length + 1)
            if "VALORANT" in buf.value:
                val_hwnd = hwnd_cand
                val_title = buf.value
                return False
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    except Exception:
        pass
    return val_hwnd, val_title

def get_window_info():
    """Return comprehensive window metrics: bounds, resolution, focus, minimized state."""
    hwnd, title = get_valorant_window()
    if not hwnd:
        return {
            "exists": False,
            "hwnd": 0,
            "title": "",
            "is_foreground": False,
            "is_minimized": False,
            "width": 0,
            "height": 0,
            "rect": [0, 0, 0, 0],
            "client_rect": [0, 0, 0, 0]
        }

    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    fg_hwnd = user32.GetForegroundWindow()
    is_fg = (fg_hwnd == hwnd)
    is_min = bool(user32.IsIconic(hwnd))

    client_rc = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client_rc))
    w = client_rc.right - client_rc.left
    h = client_rc.bottom - client_rc.top
    if w <= 0 or h <= 0:
        w = rc.right - rc.left
        h = rc.bottom - rc.top

    return {
        "exists": True,
        "hwnd": int(hwnd),
        "title": title,
        "is_foreground": is_fg,
        "is_minimized": is_min,
        "width": int(w),
        "height": int(h),
        "rect": [int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)],
        "client_rect": [int(client_rc.left), int(client_rc.top), int(client_rc.right), int(client_rc.bottom)]
    }

def focus_valorant_window():
    """Restore and bring Valorant to front."""
    hwnd, _ = get_valorant_window()
    if not hwnd:
        return False, "Valorant window not found"

    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    return True, "focused"

# ── MAP RESOLVER & AGENTS ───────────────────────────────────────────
MAP_NAMES = {
    "/Game/Maps/Ascent/Ascent": "ASCENT",
    "/Game/Maps/Bonsai/Bonsai": "SPLIT",
    "/Game/Maps/Duality/Duality": "BIND",
    "/Game/Maps/Foxtrot/Foxtrot": "BREEZE",
    "/Game/Maps/Canyon/Canyon": "FRACTURE",
    "/Game/Maps/Pitt/Pitt": "PEARL",
    "/Game/Maps/Jam/Jam": "LOTUS",
    "/Game/Maps/Jules/Jules": "SUNSET",
    "/Game/Maps/Infinity/Infinity": "ABYSS",
    "/Game/Maps/Triad/Triad": "HAVEN",
    "/Game/Maps/Port/Port": "ICEBOX",
    "/Game/Maps/Hurm/Hurm_Alley": "DISTRICT (TDM)",
    "/Game/Maps/Hurm/Hurm_Yard": "KASBAH (TDM)",
    "/Game/Maps/Hurm/Hurm_Bowl": "PIAZZA (TDM)",
    "/Game/Maps/Hurm/Hurm_Drift": "DRIFT (TDM)",
    "/Game/Maps/Hurm/Hurm_Warehouse": "GLITCH (TDM)",
}

def resolve_map_name(raw_map):
    if not raw_map:
        return "Unknown"
    for k, v in MAP_NAMES.items():
        if k.lower() in raw_map.lower():
            return v
    parts = raw_map.strip("/").split("/")
    if parts:
        return parts[-1].upper()
    return raw_map.upper()

AGENTS = {
    "Brimstone": "9f0d8ba9-4140-b941-57d3-a7ad57c6b43b",
    "Viper": "707eab51-4836-f488-046a-cda6bf494859",
    "Omen": "8e253930-4c05-31dd-1b6c-968525494517",
    "Cypher": "117ed9e3-49f3-6512-3ccf-0cada7e3823b",
    "Sova": "320b2a48-4d9b-a075-30f1-1f93a9b638fa",
    "Sage": "569fdd95-4d10-43ab-ca70-79becc718b46",
    "Phoenix": "eb93336a-449b-9c1b-0a54-a891f7921d69",
    "Jett": "add6443a-41bd-e414-f6ad-e58d267f4e95",
    "Raze": "f94c3b30-42be-e959-889c-5aa82d2879c1",
    "Reyna": "a3bfb853-43b2-7238-a4f1-ad90e9e46bcc",
    "Killjoy": "1e58de9c-4950-5125-93e9-a0aee9f98746",
    "Skye": "6f2a04ca-43e0-be17-7f36-b3908627744a",
    "Yoru": "7f94d92c-4234-0a36-9646-3a87eb8b5c89",
    "Astra": "41fb69c1-4189-7b37-f117-bcaf1e96f1bf",
    "KAY/O": "601dbbe7-43ce-be57-2a40-4abd24953621",
    "Chamber": "22697a3d-45bf-8dd7-4fec-84a9e28c69d7",
    "Neon": "bb2a4828-46eb-8cd1-e765-15848195d751",
    "Fade": "dade69b4-4f5a-8528-247b-219e5a1facd6",
    "Harbor": "95b78ed7-4637-86f9-7e41-71ba8c293152",
    "Gekko": "e370fa57-4757-3604-3648-499e1f642d3f",
    "Deadlock": "cc8b64c8-4b25-4ff9-6e7f-37b4da43d235",
    "Iso": "0e38b510-41a8-5780-5e8f-568b2a4f2d6c",
    "Clove": "1bf58e73-4b4c-5c7a-9e5a-62d98a3cbb6d",
    "Vyse": "efba5359-4016-a1e5-7626-b1ae8bbb6a39",
    "Tejo": "2fe4ef3d-4849-ade2-3ee4-fba20435dbcc",
    "Waylay": "df5e5c46-4a6c-48c0-8274-eb89d81d29c8",
    "Veto": "17743d5b-486a-4934-8b6b-4e897e685f4e",
    "Miks": "87e35b71-419b-449e-b7d8-a92c0a969b8b",
}

def find_game_exe():
    for p in GAME_EXE_PATHS:
        if os.path.exists(p):
            return p
    return None

RC_PATHS = [
    os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\RiotClientServices.exe"),
    r"C:\Program Files\Riot Games\Riot Client\RiotClientServices.exe",
    r"C:\Program Files (x86)\Riot Games\Riot Client\RiotClientServices.exe",
    r"C:\Riot Games\Riot Client\RiotClientServices.exe",
]

def find_rc_exe():
    for p in RC_PATHS:
        if os.path.exists(p):
            return p
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Riot Client") as k:
                    loc, _ = winreg.QueryValueEx(k, "InstallLocation")
                    cand = os.path.join(loc, "RiotClientServices.exe")
                    if os.path.exists(cand):
                        return cand
            except Exception:
                pass
    except Exception:
        pass
    return None

MARKERS = [
    (re.compile(r"LogPostGame|PostGame|postgame", re.I),               "postgame",     50),
    (re.compile(r"LogMapLoadModel.*Match Setup: TRUE", re.I),          "in_game",      45),
    (re.compile(r"MatchState.*(?:InProgress|Playing|Entered)", re.I),  "in_game",      40),
    (re.compile(r"LogPregameManager: .*MatchID|Initialized: PregameManager", re.I), "agent_select", 35),
    (re.compile(r"Pregame_GetPlayer|LogPregameManager", re.I),         "agent_select", 30),
    (re.compile(r"Match.?Found|FoundMatch|matchmaking.*found", re.I),  "match_found",  25),
    (re.compile(r"MM: |MatchmakingManager", re.I),                     "queued",       20),
    (re.compile(r"HomeScreen|MainMenu|TransitionToMainMenu|Party_FetchCustomGameConfigs|main/lobby|LogUINavigationModel|Entering state: MainMenu|PartyManager", re.I), "menus", 10),
    (re.compile(r"LogPlatformInitializerV2|PlatformInitializer|Beginning platform|LogGameFlowStateManager: Reconcile.*Initialization|LogInit:", re.I), "loading", 5),
]

def tail_lines(n=300):
    try:
        with open(GAME_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except Exception:
        return []

def game_running():
    win = get_window_info()
    if win["exists"]:
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + GAME_EXE, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        return GAME_EXE.lower() in out.lower()
    except Exception:
        return False

# ── FAST LOG WATCHER (background thread, 250ms tick) ────────────────
# Watches ShooterGame.log in real-time to catch state transitions
# (match_found, agent_select, in_game) without waiting for the API poll.
_log_state_cache = {
    "state": "unknown",     # fastest known state from log
    "line": "",
    "pos": 0,               # last read position in log file
}
_log_state_lock = threading.Lock()

# Log markers for fast watcher — ordered highest priority first
# The watcher scans new lines newest→oldest and takes the FIRST match.
FAST_MARKERS = [
    # ① Agent locked — earliest possible 5v5 signal, fires when lock-in completes
    #    (~3-5s before MatchState:InProgress appears in the log)
    (re.compile(r"Pregame_LockCharacter|LockCharacter", re.I), "agent_locked"),
    # ② In-game (map loading)
    (re.compile(r"MatchState.*InProgress|LogMapLoadModel.*Match Setup: TRUE", re.I), "in_game"),
    # ③ Agent select / pregame open
    (re.compile(r"LogPregameManager|Pregame_GetPlayer|Initialized: PregameManager|Pregame_SelectCharacter", re.I), "agent_select"),
    # ④ Match found — brief popup before agent select
    (re.compile(r"Match.?Found|FoundMatch|matchmaking.*found", re.I), "match_found"),
    # ⑤ Back in menus
    (re.compile(r"HomeScreen|MainMenu|TransitionToMainMenu|PartyManager|Party_FetchCustomGameConfigs", re.I), "menus"),
    # ⑥ Matchmaking
    (re.compile(r"MM: |MatchmakingManager", re.I), "queued"),
]

def _log_watcher():
    global _log_state_cache
    last_pos = 0
    while True:
        try:
            if not os.path.exists(GAME_LOG):
                time.sleep(1.0)
                continue
            sz = os.path.getsize(GAME_LOG)
            if sz < last_pos:
                last_pos = 0  # log rotated
            if sz == last_pos:
                time.sleep(0.25)
                continue
            with open(GAME_LOG, "rb") as f:
                f.seek(last_pos)
                chunk = f.read(min(sz - last_pos, 131072))
            last_pos += len(chunk)
            text = chunk.decode("utf-8", "replace")
            for line in reversed(text.splitlines()):
                for rx, name in FAST_MARKERS:
                    if rx.search(line):
                        with _log_state_lock:
                            _log_state_cache["state"] = name
                            _log_state_cache["line"] = line.strip()[:200]
                        break
                else:
                    continue
                break  # stop at first (most recent) match
        except Exception:
            pass
        time.sleep(0.25)

# Start log watcher background thread
threading.Thread(target=_log_watcher, daemon=True).start()

def get_fast_log_state():
    with _log_state_lock:
        return _log_state_cache["state"], _log_state_cache["line"]


def detect_state():
    if not game_running():
        return "offline", "?", "game not running"
    lines = tail_lines(400)
    if not lines:
        return "loading", "?", "no log data"

    # Scan all recent lines and pick the HIGHEST PRIORITY match.
    # This prevents stale low-priority lines (loading/queued) from
    # overriding a more recent high-priority state (menus/in_game).
    best_name = None
    best_pri = -1
    best_since = "?"
    best_line = ""

    for line in lines:
        for rx, name, pri in MARKERS:
            if rx.search(line) and pri > best_pri:
                m = re.search(r"\[(\d{4}\.\d{2}\.\d{2})-(\d{2}:\d{2}:\d{2}:\d{3})\]?", line)
                best_name = name
                best_pri = pri
                best_since = m.group(0) if m else "?"
                best_line = line.strip()[:200]

    if best_name:
        return best_name, best_since, best_line
    return "loading", "?", "initializing game"

def wait_lockfile(seconds):
    for _ in range(seconds * 2):
        lf = read_lockfile()
        if lf:
            return lf
        time.sleep(0.5)
    return None

_launch_in_progress = False
_last_launch_status = "idle"

def get_launch_status():
    global _last_launch_status
    return _last_launch_status

def set_launch_status(s):
    global _last_launch_status
    _last_launch_status = s
    print("[LAUNCH] %s" % s)

def find_valorant_launcher_exe():
    """Locate official VALORANT.exe bootstrapper."""
    yaml_path = r"C:\ProgramData\Riot Games\Metadata\valorant.live\valorant.live.product_settings.yaml"
    if os.path.exists(yaml_path):
        try:
            with open(yaml_path, "r", errors="ignore") as f:
                content = f.read()
            m = re.search(r'product_install_full_path:\s*["\']?([^"\']+)["\']?', content)
            if m:
                cand = os.path.join(m.group(1).replace("/", "\\"), "VALORANT.exe")
                if os.path.exists(cand):
                    return cand
        except Exception:
            pass

    for drive in ("C", "D", "E", "F"):
        candidates = [
            rf"{drive}:\Riot Games\VALORANT\live\VALORANT.exe",
            rf"{drive}:\Program Files\Riot Games\VALORANT\live\VALORANT.exe",
            rf"{drive}:\Games\Riot Games\VALORANT\live\VALORANT.exe",
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
    return None

def focus_riot_client():
    try:
        def enum_cb(hwnd, results):
            if ctypes.windll.user32.IsWindowVisible(hwnd):
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                    if "Riot Client" in buf.value:
                        results.append(hwnd)
            return True
        results = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_cb), ctypes.byref(results))
        if results:
            hwnd = results[0]
            ctypes.windll.user32.ShowWindow(hwnd, 9) # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False

def rc_trigger_launch(lf):
    """Trigger Valorant launch via Riot Client's internal Foundation API."""
    try:
        url = "https://127.0.0.1:%d/product-launcher/v1/products/valorant/patchlines/live" % lf["port"]
        auth = base64.b64encode(("riot:%s" % lf["password"]).encode()).decode()
        req = urllib.request.Request(url, data=b"{}", method="POST",
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=_CTX, timeout=3) as r:
            body = r.read().decode().strip()
            print("Valorant launched via Riot Client API: %s" % body)
            return True, "Valorant started (session %s)" % body
    except Exception as e:
        print("Riot Client API call note: %s" % e)
        return False, str(e)

def _launch_worker():
    global _launch_in_progress
    try:
        if game_running():
            set_launch_status("Game is already running")
            focus_valorant_window()
            return

        set_launch_status("Checking Riot Client status...")

        # Step 1: Check if Riot Client is already active with lockfile
        lf = read_lockfile()
        if lf:
            set_launch_status("Riot Client active (port %d). Attempting API launch..." % lf["port"])
            for attempt in range(4):
                ok, msg = rc_trigger_launch(lf)
                if ok:
                    set_launch_status("Game launched via Riot Client API: %s" % msg)
                    focus_valorant_window()
                    return
                time.sleep(1.0)

        # Step 2: Launch VALORANT.exe directly via Windows Shell
        val_exe = find_valorant_launcher_exe()
        if val_exe:
            set_launch_status("Launching VALORANT.exe (%s)..." % val_exe)
            try:
                # Use os.startfile or ShellExecuteW to avoid [WinError 5] Access is denied
                try:
                    os.startfile(val_exe)
                except Exception:
                    ctypes.windll.shell32.ShellExecuteW(None, "open", val_exe, None, os.path.dirname(val_exe), 1)
                for _ in range(10):
                    time.sleep(1.0)
                    if game_running():
                        set_launch_status("Valorant process detected and running!")
                        focus_valorant_window()
                        return
            except Exception as e:
                print("Failed spawning VALORANT.exe: %s" % e)

        # Step 3: Spawn Riot Client with patchline args
        rc_exe = find_rc_exe() or r"C:\Riot Games\Riot Client\RiotClientServices.exe"
        if os.path.exists(rc_exe):
            set_launch_status("Spawning Riot Client with patchline args...")
            try:
                try:
                    ctypes.windll.shell32.ShellExecuteW(None, "open", rc_exe, "--launch-product=valorant --launch-patchline=live", os.path.dirname(rc_exe), 1)
                except Exception:
                    subprocess.Popen([rc_exe, "--launch-product=valorant", "--launch-patchline=live"],
                                     cwd=os.path.dirname(rc_exe))
            except Exception as e:
                print("Failed spawning RiotClientServices.exe: %s" % e)

            for i in range(14):
                time.sleep(1.0)
                if game_running():
                    set_launch_status("Valorant game process running!")
                    focus_valorant_window()
                    return
                lf = read_lockfile()
                if lf:
                    ok, msg = rc_trigger_launch(lf)
                    if ok:
                        set_launch_status("Valorant launched via Riot Client API!")
                        focus_valorant_window()
                        return

        focus_riot_client()
        if game_running():
            set_launch_status("Valorant is active!")
        else:
            set_launch_status("Riot Client is open. Please verify login.")
    except Exception as e:
        set_launch_status("Launch error: %s" % e)
    finally:
        _launch_in_progress = False

def trigger_launch_async():
    global _launch_in_progress
    if game_running():
        focus_valorant_window()
        return "Game is already running"

    if not _launch_in_progress:
        _launch_in_progress = True
        t = threading.Thread(target=_launch_worker, daemon=True)
        t.start()
        return "Initiated Valorant launch sequence on clean PC"
    else:
        return "Launch sequence already in progress: %s" % _last_launch_status

def launch():
    msg = trigger_launch_async()
    return [msg], None

def kill_game():
    set_launch_status("Game terminated")
    subprocess.run(["taskkill", "/F", "/IM", GAME_EXE], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "VALORANT.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "RiotClientServices.exe"], capture_output=True)

# ── RIOT LOCAL + GLZ API ─────────────────────────────────────────────
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

def read_lockfile():
    try:
        if not os.path.exists(LOCKFILE):
            return None
        with open(LOCKFILE, "r") as f:
            parts = f.read().strip().split(":")
        if len(parts) >= 5:
            pid = int(parts[1])
            port = int(parts[2])
            password = parts[3]
            protocol = parts[4]
            # Verify process is still alive so we don't connect to a dead port
            try:
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if not h:
                    return None
                ctypes.windll.kernel32.CloseHandle(h)
            except Exception:
                pass
            return {"name": parts[0], "pid": pid, "port": port, "password": password, "protocol": protocol}
    except Exception:
        pass
    return None

def read_rso():
    path = os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\Data\RiotGamesPrivateSettings.yaml")
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
        id_tok = re.search(r'id_token:\s*"([^"]+)"', text)
        rf_tok = re.search(r'refresh_token:\s*"([^"]+)"', text)
        return {"id_token": id_tok.group(1) if id_tok else "",
                "refresh_token": rf_tok.group(1) if rf_tok else ""}, None
    except Exception as e:
        return None, "RSO read failed: %s" % e

def _open(req, timeout=15):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)

def rso_refresh():
    rso, err = read_rso()
    if err:
        return None, err
    rt = (rso or {}).get("refresh_token")
    if not rt:
        return None, "no refresh_token in RiotGamesPrivateSettings.yaml"
    import urllib.parse
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": "riot-client",
        "scope": "openid link ban lol_region lo l account",
    }).encode()
    req = urllib.request.Request("https://auth.riotgames.com/token", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _open(req) as r:
            data = json.loads(r.read().decode())
        tok = data.get("access_token") or data.get("id_token")
        if not tok:
            return None, "refresh response missing access_token: %s" % str(data)[:200]
        return tok, None
    except Exception as e:
        return None, "refresh failed: %s" % e

def rso_entitlements(access_token):
    req = urllib.request.Request("https://entitlements.auth.riotgames.com/api/token/v1",
        data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + access_token,
                 "Content-Type": "application/json"})
    try:
        with _open(req) as r:
            data = json.loads(r.read().decode())
        ent = data.get("entitlements_token")
        if not ent:
            return None, "no entitlements_token: %s" % str(data)[:200]
        return ent, None
    except Exception as e:
        return None, "entitlements failed: %s" % e

CLIENT_VERSION = "release-13.05-shipping-11-5350494"
CLIENT_PLATFORM = base64.b64encode(json.dumps({
    "platformType": "PC",
    "platformOS": "Windows",
    "platformOSVersion": "10.0.26200.1.768.64bit",
    "platformChipset": "Unknown",
}).encode()).decode()

def glz_headers():
    tok, err = rso_refresh()
    if err or not tok:
        return None, err or "no access token"
    ent, err2 = rso_entitlements(tok)
    if err2 or not ent:
        return None, err2 or "no entitlements"
    return {
        "Authorization": "Bearer " + tok,
        "X-Riot-Entitlements-JWT": ent,
        "X-Riot-ClientVersion": CLIENT_VERSION,
        "X-Riot-ClientPlatform": CLIENT_PLATFORM,
    }, None

def region_of():
    rso, err = read_rso()
    id_tok = (rso or {}).get("id_token", "")
    if err or not id_tok:
        return "eu", None
    try:
        jwt = id_tok.split(".")[1]
        jwt += "=" * (-len(jwt) % 4)
        payload = json.loads(base64.urlsafe_b64decode(jwt))
        c = payload.get("dat", {}).get("c", "ue1")
        m = re.match(r"([a-z]{2})\d", c)
        r = m.group(1) if m else "eu"
        if r == "ue":
            r = "eu"
        return r, None
    except Exception:
        return "eu", None

def glz(verb, path, body=None):
    hdrs, err = glz_headers()
    if err:
        return None, err
    region, _ = region_of()
    url = "https://glz-%s-1.%s.a.pvp.net%s" % (region, region, path)
    cmd = ["curl.exe", "-s", "-X", verb, url,
           "-H", "Authorization: Bearer " + hdrs["Authorization"].split()[1],
           "-H", "X-Riot-Entitlements-JWT: " + hdrs["X-Riot-Entitlements-JWT"],
           "-H", "X-Riot-ClientVersion: " + hdrs["X-Riot-ClientVersion"],
           "-H", "X-Riot-ClientPlatform: " + hdrs["X-Riot-ClientPlatform"],
           "-H", "User-Agent: RiotClient/53.0.1.4742290.0 rso-auth"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None, "curl failed: %s" % r.stderr[:150]
        out = r.stdout.strip()
        if not out:
            return {}, None
        return json.loads(out), None
    except Exception as e:
        return None, "glz error: %s" % e

def local_api(path, method="GET", body=None):
    """Call the local Riot Client API using the lockfile credentials."""
    lf = read_lockfile()
    if not lf:
        return None, "lockfile not found"
    port = lf["port"]
    pw = lf["password"]
    creds = base64.b64encode(("riot:" + pw).encode()).decode()
    url = "https://127.0.0.1:%d%s" % (port, path)
    headers = {
        "Authorization": "Basic " + creds,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_CTX),
            urllib.request.ProxyHandler({})
        )
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read().decode()), None
    except Exception as e:
        return None, "local_api error: %s" % e

_cached_puuid = None

def get_puuid():
    """Get player PUUID from local Riot Client session API."""
    global _cached_puuid
    if _cached_puuid:
        return _cached_puuid, None
    data, err = local_api("/chat/v1/session")
    if err or not data:
        return None, err or "no session"
    puuid = data.get("puuid") or data.get("game_puuid")
    if not puuid:
        return None, "no puuid in session: %s" % str(data)[:120]
    _cached_puuid = puuid
    return puuid, None

def party_id():
    """Get the player's current party ID via the correct GLZ endpoint."""
    puuid, err = get_puuid()
    if err or not puuid:
        return None, err or "no puuid"
    data, err = glz("GET", "/parties/v1/players/%s" % puuid)
    if err or not data:
        return None, err or "no party data"
    pid = data.get("CurrentPartyID") or data.get("PartyID") or data.get("ID") or data.get("id")
    if not pid:
        return None, "no party ID in response: %s" % str(data)[:120]
    return pid, None

def set_queue_mode(mode):
    pid, err = party_id()
    if err or not pid:
        print("[QUEUE] party_id failed: %s" % err)
        return {"ok": False, "error": err or "no party"}
    data, err = glz("POST", "/parties/v1/parties/%s/queue" % pid, {"queueID": mode})
    print("[QUEUE] set_queue_mode %s -> err=%s resp=%s" % (mode, err, str(data)[:120]))
    return {"ok": err is None, "party": pid, "queue": mode, "resp": data, "error": err}

def start_queue(mode):
    pid, err = party_id()
    if err or not pid:
        print("[QUEUE] party_id failed: %s" % err)
        return {"ok": False, "error": err or "no party"}
    # 1. Set the game mode / queue on the party
    data1, err1 = glz("POST", "/parties/v1/parties/%s/queue" % pid, {"queueID": mode})
    print("[QUEUE] set queue %s -> err=%s resp=%s" % (mode, err1, str(data1)[:120]))
    # 2. Enter the matchmaking queue
    data2, err2 = glz("POST", "/parties/v1/parties/%s/matchmaking/join" % pid, {})
    print("[QUEUE] matchmaking/join -> err=%s resp=%s" % (err2, str(data2)[:120]))
    return {"ok": err2 is None, "party": pid, "queue": mode,
            "set_resp": data1, "join_resp": data2, "error": err2 or err1}

def cancel_queue():
    pid, err = party_id()
    if err or not pid:
        return {"ok": False, "error": err or "no party"}
    data, err = glz("POST", "/parties/v1/parties/%s/matchmaking/leave" % pid, {})
    return {"ok": err is None, "party": pid, "resp": data, "error": err}

def pick_agent(name):
    agent_id = AGENTS.get(name)
    if not agent_id:
        return {"ok": False, "error": "unknown agent: %s" % name}
    puuid, err = get_puuid()
    if err or not puuid:
        return {"ok": False, "error": "no puuid: %s" % err}
    data, err = glz("GET", "/pregame/v1/players/%s" % puuid)
    if err or not data:
        return {"ok": False, "error": err or "no pregame data"}
    mid = data.get("MatchID") or data.get("matchId")
    if not mid:
        return {"ok": False, "error": "not in a pregame match (puuid=%s)" % puuid}
    print("[PICK] Agent %s (%s) match=%s" % (name, agent_id, mid[:8]))
    # Agent ID goes in the URL path, no body needed
    sel, err1 = glz("POST", "/pregame/v1/matches/%s/select/%s" % (mid, agent_id), {})
    print("[PICK] select -> err=%s resp=%s" % (err1, str(sel)[:120]))
    lock, err2 = glz("POST", "/pregame/v1/matches/%s/lock/%s" % (mid, agent_id), {})
    print("[PICK] lock   -> err=%s resp=%s" % (err2, str(lock)[:120]))
    return {"ok": err1 is None and err2 is None, "match": mid, "agent": name,
            "select_err": err1, "lock_err": err2}

# ── HTTP REQUEST HANDLER ─────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj):
        try:
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/status", "/state"):
                win = get_window_info()
                running = game_running() or win["exists"]

                map_name = "Unknown"
                pregame_mid = None
                coregame_mid = None
                ready_to_play = False
                queue_elapsed_secs = -1  # -1 = not in queue

                puuid, _ = get_puuid() if running else (None, None)

                if running and puuid:
                    # ── Party / queue state ────────────────────────────────
                    try:
                        pid, perr = party_id()
                        if pid:
                            ready_to_play = True
                            # Get actual queue start time from party MatchmakingData
                            pdata, _ = glz("GET", "/parties/v1/parties/%s" % pid)
                            if pdata:
                                mm = pdata.get("MatchmakingData") or {}
                                qet = mm.get("QueueEntryTime", "")  # ISO timestamp
                                if qet and qet != "0001-01-01T00:00:00Z":
                                    import datetime
                                    try:
                                        # Parse ISO 8601 and compute elapsed seconds
                                        qet_clean = qet.split(".")[0].rstrip("Z")
                                        qt = datetime.datetime.strptime(qet_clean, "%Y-%m-%dT%H:%M:%S")
                                        now = datetime.datetime.utcnow()
                                        queue_elapsed_secs = max(0, int((now - qt).total_seconds()))
                                    except Exception:
                                        pass
                    except Exception:
                        pass

                    # ── Pregame (agent select) ─────────────────────────────
                    try:
                        p_pre, _ = glz("GET", "/pregame/v1/players/%s" % puuid)
                        if p_pre and (p_pre.get("MatchID") or p_pre.get("matchId")):
                            pregame_mid = p_pre.get("MatchID") or p_pre.get("matchId")
                            match_data, _ = glz("GET", "/pregame/v1/matches/%s" % pregame_mid)
                            if match_data:
                                map_name = resolve_map_name(match_data.get("MapID", ""))
                    except Exception:
                        pass

                    # ── Coregame (in active match / 5v5 screen) ────────────
                    if not pregame_mid:
                        try:
                            cg, _ = glz("GET", "/core-game/v1/players/%s" % puuid)
                            if cg and (cg.get("MatchID") or cg.get("matchId")):
                                coregame_mid = cg.get("MatchID") or cg.get("matchId")
                                cg_data, _ = glz("GET", "/core-game/v1/matches/%s" % coregame_mid)
                                if cg_data:
                                    map_name = resolve_map_name(cg_data.get("MapID", ""))
                        except Exception:
                            pass

                # ── Determine authoritative state ──────────────────────────
                fast_state, fast_line = get_fast_log_state()

                if pregame_mid:
                    state = "agent_select"
                    since = "live-pregame"
                    line = "Pregame match: " + str(pregame_mid)
                elif coregame_mid:
                    state = "in_game"
                    since = "live-coregame"
                    line = "Coregame match: " + str(coregame_mid)
                    ready_to_play = False
                elif fast_state == "agent_locked" and running:
                    # Pregame_LockCharacter fired — agent just locked, map loading imminent
                    state = "agent_locked"
                    since = "log-fast"
                    line = fast_line
                    ready_to_play = False
                elif fast_state == "in_game" and running:
                    state = "in_game"
                    since = "log-fast"
                    line = fast_line
                    ready_to_play = False
                elif fast_state == "agent_select" and running:
                    state = "agent_select"
                    since = "log-fast"
                    line = fast_line
                else:
                    state, since, line = detect_state()
                    if state in ("menus", "LOBBY"):
                        ready_to_play = True
                    elif ready_to_play and state in ("loading", "offline"):
                        state = "menus"

                # Loading only if game is running and truly loading
                if state in ("menus", "LOBBY", "agent_select", "in_game", "queued", "match_found", "postgame") or ready_to_play:
                    is_loading = False
                elif running and state == "loading":
                    is_loading = True
                else:
                    is_loading = False

                self._send({
                    "ok": True,
                    "game": running,
                    "loading": is_loading,
                    "ready_to_play": ready_to_play or (state == "menus"),
                    "launch_status": get_launch_status(),
                    "window": win,
                    "state": state,
                    "state_since": since,
                    "last_state_line": line,
                    "map": map_name,
                    "pregame_id": pregame_mid,
                    "coregame_id": coregame_mid,
                    "queue_elapsed_secs": queue_elapsed_secs,
                })
            elif path == "/window/focus":
                ok, msg = focus_valorant_window()
                self._send({"ok": ok, "message": msg, "window": get_window_info()})
            elif path == "/launch":
                launched, err = launch()
                if err:
                    self._send({"ok": False, "error": err})
                    return
                self._send({"ok": True, "launched": launched})
            elif path == "/kill":
                kill_game()
                self._send({"ok": True, "killed": True})
            elif path == "/log":
                n = 50
                try: n = int(self.path.split("n=")[1].split("&")[0])
                except Exception: pass
                self._send({"ok": True, "lines": tail_lines(n)})
            elif path == "/set_mode":
                mode = "unrated"
                try: mode = self.path.split("mode=")[1].split("&")[0]
                except Exception: pass
                if mode not in ("unrated", "competitive", "swiftplay", "spikerush", "deathmatch", "hurm", "custom"):
                    self._send({"ok": False, "error": "bad mode"})
                    return
                self._send(set_queue_mode(mode))
            elif path == "/queue":
                mode = "unrated"
                try: mode = self.path.split("mode=")[1].split("&")[0]
                except Exception: pass
                if mode not in ("unrated", "competitive", "swiftplay", "spikerush", "deathmatch", "hurm", "custom"):
                    self._send({"ok": False, "error": "bad mode"})
                    return
                self._send(start_queue(mode))
            elif path == "/cancel":
                self._send(cancel_queue())
            elif path == "/pick":
                agent = "Jett"
                try: agent = self.path.split("agent=")[1].split("&")[0]
                except Exception: pass
                self._send(pick_agent(agent))
            elif path == "/session":
                lf = read_lockfile()
                self._send({
                    "ok": True,
                    "lockfile": lf,
                    "region": region_of()[0],
                    "window": get_window_info()
                })
            else:
                self._send({
                    "ok": False,
                    "error": "unknown endpoint",
                    "endpoints": ["/status", "/window/focus", "/launch", "/kill", "/log",
                                  "/queue", "/cancel", "/pick", "/session"]
                })
        except Exception as e:
            self._send({"ok": False, "error": str(e)})

def main():
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("clean_agent listening on 0.0.0.0:%d" % PORT)
    print("win32 window inspection: active")
    print("endpoints: /status /window/focus /launch /kill /log /queue /cancel /pick")
    srv.serve_forever()

if __name__ == "__main__":
    main()
