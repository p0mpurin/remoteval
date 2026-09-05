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
QUEUE_MODES = {"unrated": "Unrated", "competitive": "Competitive", "swiftplay": "Swiftplay",
               "spikerush": "Spike Rush", "deathmatch": "Deathmatch", "hurm": "Team Deathmatch"}
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
AGENT_NAMES_BY_ID = {agent_id.lower(): name for name, agent_id in AGENTS.items()}

def sanitize_pregame_roster(payload, self_subject):
    """Reduce Riot's pregame match response to the five ally slots the UI needs."""
    if not isinstance(payload, dict):
        return []
    ally = payload.get("AllyTeam") or {}
    players = ally.get("Players") if isinstance(ally, dict) else None
    if not isinstance(players, list):
        return []
    result = []
    for slot, player in enumerate(players[:5]):
        if not isinstance(player, dict):
            continue
        subject = player.get("Subject")
        character_id = player.get("CharacterID") or ""
        selection = str(player.get("CharacterSelectionState") or "").lower()
        locked = "lock" in selection
        state = "LOCKED" if locked else "HOVERING" if character_id else "CHOOSING"
        result.append({
            "slot": slot,
            "subject": subject if isinstance(subject, str) else "",
            "self": bool(subject and subject == self_subject),
            "agent_id": character_id if isinstance(character_id, str) else "",
            "agent": AGENT_NAMES_BY_ID.get(str(character_id).lower(), ""),
            "state": state,
            "locked": locked,
        })
    return result

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
# Log tailing is diagnostic only; status uses the embedded detector.

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
    if detector is None:
        return None, "state detector not started"
    with detector.auth_lock:
        auth = detector.auth
    if not auth or auth[3] <= time.time():
        detector.refresh.set()
        return None, "Riot Client authentication not ready"
    return dict(auth[1]), None


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
    headers, err = glz_headers()
    if err:
        return None, err
    with detector.auth_lock:
        if time.monotonic() < detector.remote_pause_until:
            return None, "Riot rate limit cooldown active"
    cfg = detector.config
    url = "https://glz-%s-1.%s.a.pvp.net%s" % (cfg.region, cfg.shard, path)
    try:
        with _HttpSession() as session:
            response = session.request(verb, url, headers=headers, body=body, timeout=(1., 2.))
            if response.status_code == 401:
                detector.refresh.set()
            if response.status_code == 429:
                with detector.auth_lock:
                    detector.remote_pause_until = max(detector.remote_pause_until,
                        time.monotonic() + detector._retry_after(response.headers.get("Retry-After")))
            response.raise_for_status()
            return response.json() if response.body else {}, None
    except (_NetworkError, ValueError):
        return None, "GLZ request failed; verify authentication, region, and client version"


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
    if detector is None:
        return None, "state detector not started"
    with detector.auth_lock:
        auth = detector.auth
    return (auth[0], None) if auth else (None, "Riot Client authentication not ready")


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
    if err1:
        return {"ok": False, "error": err1}
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
    if err1:
        return {"ok": False, "match": mid, "agent": name, "select_err": err1}
    lock, err2 = glz("POST", "/pregame/v1/matches/%s/lock/%s" % (mid, agent_id), {})
    print("[PICK] lock   -> err=%s resp=%s" % (err2, str(lock)[:120]))
    return {"ok": err1 is None and err2 is None, "match": mid, "agent": name,
            "select_err": err1, "lock_err": err2}

def get_presence_state():
    """
    Directly query local Riot Client /chat/v4/presences.
    Decodes the 'private' base64 payload to read exact in-memory game state:
      - sessionLoopState: 'MENUS' (lobby), 'PREGAME' (agent select), 'INGAME' (5v5 screen / match)
      - partyState: 'DEFAULT', 'MATCHMAKING' (in queue), 'MATCHMADE_GAME_STARTING' (match found)
      - queueEntryTime: exact queue start timestamp
      - matchMap: current map
    Returns dict or None.
    """
    try:
        data, err = local_api("/chat/v4/presences")
        if err or not data or "presences" not in data:
            return None

        for p in data.get("presences", []):
            priv_b64 = p.get("private")
            if not priv_b64:
                continue

            try:
                rem = len(priv_b64) % 4
                if rem > 0:
                    priv_b64 += "=" * (4 - rem)
                raw = base64.b64decode(priv_b64)
                priv = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                try:
                    raw = base64.urlsafe_b64decode(priv_b64)
                    priv = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue

            if not isinstance(priv, dict):
                continue

            loop_state = priv.get("sessionLoopState")  # MENUS, PREGAME, INGAME
            party_state = priv.get("partyState")        # DEFAULT, MATCHMAKING, MATCHMADE_GAME_STARTING

            if not loop_state and not party_state:
                continue

            # Map
            map_path = priv.get("matchMap") or priv.get("partyOwnerMatchMap") or ""
            map_name = resolve_map_name(map_path) if map_path else "Unknown"

            # Queue elapsed seconds
            queue_elapsed = -1
            qet = priv.get("queueEntryTime") or ""
            if not qet and isinstance(priv.get("matchmakingData"), dict):
                qet = priv["matchmakingData"].get("queueEntryTime") or ""
            if not qet and isinstance(priv.get("partyData"), dict):
                qet = priv["partyData"].get("queueEntryTime") or ""

            if qet and qet != "0001-01-01T00:00:00Z":
                import datetime
                try:
                    qet_clean = qet.split(".")[0].rstrip("Z")
                    qt = datetime.datetime.strptime(qet_clean, "%Y-%m-%dT%H:%M:%S")
                    now = datetime.datetime.utcnow()
                    queue_elapsed = max(0, int((now - qt).total_seconds()))
                except Exception:
                    pass

            return {
                "loop_state": loop_state,
                "party_state": party_state,
                "queue_elapsed": queue_elapsed,
                "map_name": map_name,
                "queue_id": priv.get("queueId"),
            }
    except Exception:
        pass
    return None

# ── EMBEDDED CACHED STATE DETECTOR (standard library only) ──
import base64
import copy
import datetime as dt
import email.utils
import http.server
import json
import os
import random
import re
import threading
import time
import uuid
import zlib
import socket
import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

import http.client
import urllib.parse
import types


class _NetworkError(Exception):
    pass


class _Response:
    def __init__(self, status, headers, body):
        self.status_code, self.headers, self.body = status, headers, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _NetworkError("HTTP %d" % self.status_code)

    def json(self):
        return json.loads(self.body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _HttpSession:
    """One persistent connection per worker; system proxies are never consulted."""
    def __init__(self):
        self.auth = None
        self.connection = None
        self.destination = None

    def close(self):
        if self.connection:
            self.connection.close()
        self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get(self, url, headers=None, verify=True, timeout=(.5, .7)):
        return self.request("GET", url, headers=headers, verify=verify, timeout=timeout)

    def request(self, method, url, headers=None, verify=True, timeout=(.5, .7), body=None):
        parsed = urllib.parse.urlsplit(url)
        if not verify and parsed.hostname != "127.0.0.1":
            raise ValueError("TLS verification may only be disabled for loopback")
        destination = (parsed.scheme, parsed.hostname, parsed.port, verify)
        outgoing = dict(headers or {})
        if self.auth:
            outgoing["Authorization"] = "Basic " + base64.b64encode(
                (self.auth[0] + ":" + self.auth[1]).encode()).decode()
        if body is not None:
            body = json.dumps(body).encode()
            outgoing["Content-Type"] = "application/json"
        try:
            if self.destination != destination or self.connection is None:
                self.close()
                if parsed.scheme == "https":
                    context = ssl.create_default_context() if verify else ssl._create_unverified_context()
                    self.connection = http.client.HTTPSConnection(parsed.hostname, parsed.port,
                                                                  timeout=timeout[0], context=context)
                elif parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
                    self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout[0])
                else:
                    raise ValueError("unsupported endpoint")
                self.destination = destination
            conn = self.connection
            if conn.sock is None:
                conn.connect()
            conn.sock.settimeout(timeout[1])
            path = parsed.path + (("?" + parsed.query) if parsed.query else "")
            conn.request(method, path, body=body, headers=outgoing)
            response = conn.getresponse()
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError("response exceeds size limit")
            return _Response(response.status, response.headers, payload)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.close()
            raise _NetworkError(type(exc).__name__) from None





def _shipping_processes():
    """Toolhelp enumeration plus creation time prevents PID-reuse confusion."""
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError("process snapshot failed")
    matches = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.lower() == "valorant-win64-shipping.exe":
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if not handle:
                    # Non-admin or access denied: still track the PID with 0 creation timestamp
                    matches.append((entry.th32ProcessID, 0))
                else:
                    try:
                        times = [wintypes.FILETIME() for _ in range(4)]
                        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                            created = 0
                        else:
                            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                        matches.append((entry.th32ProcessID, created))
                    finally:
                        kernel.CloseHandle(handle)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise OSError("process enumeration incomplete")
    finally:
        kernel.CloseHandle(snapshot)
    return tuple(sorted(matches)) or None


# White PLAY glyphs from the user's 1920x1080 reference, x=868,y=0,w=128,h=56.
# Embedded binary mask, not an external image/dependency. Matching is conservative
# and intentionally limited to this English red-banner layout.
_PLAY_MASK = zlib.decompress(base64.b64decode(
    "eNrt1tEKgCAMQNHt/386CCJ00zKdhtz7VkinskQRIiL6X3qXnvIG2RF27Hf/uk6zL6N8ffDTG1jt6z5+gjb43bz3JMa3v4jgn4eL"
    + "/X6+0dfaPYX6+VzjD+HfrD/+vzbUr66/+PkMyZCtx9v9xyy/dOEJfvXUYt/5QvD397PloLgFmeMX3wt+sK+xPhEREUV0ABKHBDI="))
_PLAY_POINTS = [(i % 128, i // 128) for i, pixel in enumerate(_PLAY_MASK) if pixel]


def play_banner_score(bgra):
    if len(bgra) != 128*56*4:
        return 0.
    white = bytearray(128*56)
    red = 0
    for i in range(128*56):
        b, g, r = bgra[i*4:i*4+3]
        white[i] = min(r, g, b) > 185
        red += r > 130 and r > g*1.5 and r > b*1.3
    if red < 128*56*.5:
        return 0.
    total = sum(white)
    best = 0.
    for dy in range(-3, 4):
        for dx in range(-5, 6):
            overlap = sum(white[(y+dy)*128+x+dx] for x,y in _PLAY_POINTS
                          if 0 <= x+dx < 128 and 0 <= y+dy < 56)
            best = max(best, 2.*overlap/(len(_PLAY_POINTS)+total))
    return best


def capture_play_banner(identity):
    """Render the game HWND into a private bitmap; never sample desktop pixels."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.GetDC.argtypes = [wintypes.HWND]
    user.GetDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi.PatBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                    ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                    ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG), ("yppm", wintypes.LONG),
                    ("colors", wintypes.DWORD), ("important", wintypes.DWORD)]
    gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    if not identity:
        return None
    handles = []
    allowed = {p[0] for p in identity}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    def inspect(hwnd, unused):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(128)
        user.GetClassNameW(hwnd, name, len(name))
        if pid.value in allowed and name.value == "UnrealWindow" and not user.IsIconic(hwnd):
            handles.append(hwnd)
            return False
        return True
    user.EnumWindows(callback_type(inspect), 0)
    if not handles:
        return None
    hwnd = handles[0]
    rect = RECT()
    if not user.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right, rect.bottom
    if width < 640 or height < 360 or not 1.7 < width/height < 1.85:
        return None
    screen = user.GetDC(hwnd)  # compatibility only; its pixels are never copied
    memory = gdi.CreateCompatibleDC(screen)
    bitmap = gdi.CreateCompatibleBitmap(screen, 128, 56)
    full_dc = gdi.CreateCompatibleDC(screen)
    full_bitmap = gdi.CreateCompatibleBitmap(screen, width, height)
    if not screen or not memory or not bitmap or not full_dc or not full_bitmap:
        if full_bitmap: gdi.DeleteObject(full_bitmap)
        if full_dc: gdi.DeleteDC(full_dc)
        if bitmap: gdi.DeleteObject(bitmap)
        if memory: gdi.DeleteDC(memory)
        if screen: user.ReleaseDC(hwnd, screen)
        return None
    old = gdi.SelectObject(memory, bitmap)
    full_old = gdi.SelectObject(full_dc, full_bitmap)
    try:
        gdi.PatBlt(full_dc, 0, 0, width, height, 0x00000042)  # clear to black
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT. Occluding windows are not drawn.
        if not user.PrintWindow(hwnd, full_dc, 3):
            return None
        gdi.SetStretchBltMode(memory, 4)  # HALFTONE scaling for 720p/768p/1080p
        ok = gdi.StretchBlt(memory, 0, 0, 128, 56, full_dc,
                            round(width*868/1920), 0,
                            round(width*128/1920), round(height*56/1080), 0x00CC0020)
        gdi.SelectObject(memory, old)
        old = None
        if not ok:
            return None
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), 128, -56, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(128*56*4)
        if gdi.GetDIBits(memory, bitmap, 0, 56, buffer, ctypes.byref(header), 0) != 56:
            return None
        pixels = buffer.raw
        if not any(pixels[i] for i in range(len(pixels)) if i % 4 != 3):
            return None  # GPU backend did not provide content; no desktop fallback
        return pixels
    finally:
        if old: gdi.SelectObject(memory, old)
        gdi.SelectObject(full_dc, full_old)
        gdi.DeleteObject(full_bitmap)
        gdi.DeleteDC(full_dc)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(hwnd, screen)



def timestamp(value):
    """Accept timezone-qualified ISO 8601 only; never invent a queue start."""
    try:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = dt.datetime.strptime(value, "%Y.%m.%d-%H.%M.%S").replace(tzinfo=dt.timezone.utc)
        if parsed.year < 2000:
            return None
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def normalize_presence(private):
    """Support Riot's current nested schema and the legacy flat schema."""
    if not isinstance(private, dict) or private.get("isValid") is False:
        raise ValueError("invalid_presence")
    match = private.get("matchPresenceData") or {}
    party = private.get("partyPresenceData") or {}
    if not isinstance(match, dict) or not isinstance(party, dict):
        raise ValueError("invalid_presence_sections")
    result = {}
    for key, section in (("sessionLoopState", match), ("partyState", party), ("queueEntryTime", party)):
        value = section.get(key, private.get(key))
        if value is not None:
            result[key] = value
    if result.get("sessionLoopState") not in ("MENUS", "PREGAME", "INGAME"):
        raise ValueError("unknown_presence_state_schema")
    result["schema"] = "nested" if match or party else "flat"
    return result


class RiotEvents:
    """Minimal bounded RFC6455 text client, exclusively for Riot's loopback API."""
    def __init__(self, port, password):
        self.sock = None
        self.buffer = bytearray()
        self.fragments = bytearray()
        self.fragmenting = False
        try:
            raw = socket.create_connection(("127.0.0.1", int(port)), timeout=1.)
            self.sock = raw
            self.sock = ssl._create_unverified_context().wrap_socket(raw, server_hostname="127.0.0.1")
            self.sock.settimeout(1.)
            key = base64.b64encode(os.urandom(16)).decode()
            credentials = base64.b64encode(("riot:"+password).encode()).decode()
            self.sock.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{int(port)}\r\n"
                               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                               f"Authorization: Basic {credentials}\r\n\r\n").encode())
            header = bytearray()
            while b"\r\n\r\n" not in header:
                chunk = self.sock.recv(4096)
                if not chunk or len(header) > 65536:
                    raise ValueError("websocket_handshake_failed")
                header.extend(chunk)
            lines, tail = bytes(header).split(b"\r\n\r\n", 1)
            parts = lines.decode("latin1").split("\r\n")
            fields = dict((k.strip().lower(), v.strip()) for k,v in
                          (line.split(":",1) for line in parts[1:] if ":" in line))
            expected = base64.b64encode(hashlib.sha1((key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (parts[0].split()[1] != "101" or fields.get("sec-websocket-accept") != expected or
                fields.get("upgrade", "").lower() != "websocket"):
                raise ValueError("websocket_upgrade_rejected")
            self.buffer.extend(tail)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, payload, opcode=1):
        if isinstance(payload, str):
            payload = payload.encode()
        mask = os.urandom(4)
        size = len(payload)
        head = bytes((0x80 | opcode, 0x80 | size)) if size < 126 else bytes((0x80 | opcode, 0xFE))+struct.pack('!H',size)
        self.sock.sendall(head+mask+bytes(b ^ mask[i % 4] for i,b in enumerate(payload)))

    def receive(self):
        while True:
            if len(self.buffer) >= 2:
                first, second = self.buffer[:2]
                opcode, final = first & 15, bool(first & 128)
                if first & 0x70 or second & 128:
                    raise ValueError("invalid_websocket_frame")
                length, pos = second & 127, 2
                extended = 2 if length == 126 else 8 if length == 127 else 0
                if len(self.buffer) >= pos+extended:
                    if extended:
                        length = int.from_bytes(self.buffer[pos:pos+extended], 'big')
                        pos += extended
                    if length > 2*1024*1024 or (opcode >= 8 and (not final or length > 125)):
                        raise ValueError("oversized_websocket_frame")
                    if len(self.buffer) >= pos+length:
                        payload = bytes(self.buffer[pos:pos+length])
                        del self.buffer[:pos+length]
                        if opcode == 8:
                            raise OSError("websocket_closed")
                        if opcode == 9:
                            self.send(payload, 10)
                            continue
                        if opcode == 10:
                            continue
                        if opcode == 1 and not self.fragmenting:
                            self.fragments.clear()
                            self.fragmenting = True
                        elif opcode != 0 or not self.fragmenting:
                            raise ValueError("unexpected_websocket_opcode")
                        self.fragments.extend(payload)
                        if len(self.fragments) > 2*1024*1024:
                            raise ValueError("oversized_websocket_message")
                        if final:
                            self.fragmenting = False
                            message = self.fragments.decode('utf-8')
                            self.fragments.clear()
                            return message
                        continue
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None  # preserve partial frames across idle timeouts
            if not chunk:
                raise OSError("websocket_closed")
            self.buffer.extend(chunk)


def jwt_exp(token):
    try:
        body = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"])
    except (ValueError, KeyError, IndexError, TypeError):
        return time.time() + 60


@dataclass(frozen=True)
class Config:
    region: str
    shard: str
    client_version: str
    local_interval: float = .20
    transition_interval: float = .50
    idle_interval: float = 1.0
    freshness: float = 6.0

    def __post_init__(self):
        if not all(re.fullmatch(r"[a-z0-9]+", x) for x in (self.region, self.shard)):
            raise ValueError("Explicit region and shard required")
        if not self.client_version or self.transition_interval < .2:
            raise ValueError("Provide current client version; minimum polling interval is .2s")


class StateStore:
    """All reducer operations are short and performed under one lock."""
    def __init__(self, freshness=6.0):
        self.lock = threading.RLock()
        self.freshness = freshness
        self.instance = str(uuid.uuid4())
        self.generation = 0
        self.identity = None
        self.process_at = 0.
        self.sources = {}
        self.errors = {}
        self.state = "OFFLINE"
        self.seq = 0
        self.since = time.time()
        self.transition_mono = time.monotonic()
        self.alert = None
        self.alert_seq = 0
        self.queue_anchor = None
        self.presence_baseline = None
        self.presence_changed = False
        self.menu_samples = 0
        self.lobby_verified_at = 0.

    def touch_process(self):
        with self.lock:
            self.process_at = time.monotonic()

    def process(self, identity):
        with self.lock:
            self.process_at = time.monotonic()
            if identity != self.identity:
                self.identity = identity
                self.generation += 1
                self.sources.clear()
                self.errors.clear()
                self.queue_anchor = None
                self.presence_baseline = None
                self.presence_changed = False
                self.menu_samples = 0
                self.lobby_verified_at = 0.
                self._transition("LOADING" if identity else "OFFLINE", "process")

    def invalidate_session(self):
        with self.lock:
            self.generation += 1
            self.sources.clear()
            self.queue_anchor = None
            self.presence_baseline = None
            self.presence_changed = False
            self.menu_samples = 0
            self.lobby_verified_at = 0.
            self._transition("LOADING" if self.identity else "OFFLINE", "session_changed")

    def observe(self, source, data, generation, started=None):
        with self.lock:
            if generation != self.generation or not self.identity:
                return  # late response from a previous game/account
            now = time.monotonic()
            started = now if started is None else started
            old = self.sources.get(source)
            if old and old[0] > started:
                return
            self.sources[source] = (started, data)
            self.errors.pop(source, None)
            if source == "presence":
                if data.get("sessionLoopState") == "MENUS" and data.get("partyState") == "DEFAULT":
                    self.menu_samples = min(2, self.menu_samples + 1) if old and now-old[0] <= self.freshness else 1
                else:
                    self.menu_samples = 0
                digest = json.dumps(data, sort_keys=True)
                if self.presence_baseline is None:
                    self.presence_baseline = digest
                elif digest != self.presence_baseline:
                    self.presence_changed = True
            self._reduce(now)

    def error(self, source, message):
        with self.lock:
            self.errors[source] = message  # never include headers or response bodies

    def verify_lobby(self, generation):
        """Call ONLY from an actual current-frame lobby verifier, every <= .5s."""
        with self.lock:
            if generation == self.generation and self.identity:
                self.lobby_verified_at = time.monotonic()

    def _transition(self, state, source):
        if state == self.state:
            return
        self.state = state
        self.seq += 1
        self.since = time.time()
        self.transition_mono = time.monotonic()
        if state == "IN_GAME":
            self.alert_seq += 1
            self.alert = {"id": self.alert_seq, "kind": "CORE_GAME_ENTERED",
                          "generation": self.generation, "detected_at": self.since,
                          "source": source, "visual_screen_confirmed": False}

    def _reduce(self, now):
        fresh = {k: v for k, v in self.sources.items() if now - v[0] <= self.freshness}
        presence = fresh.get("presence", (0, {}))[1] or {}
        core = fresh.get("core", (0, None))[1]
        pre = fresh.get("pregame", (0, None))[1]
        loop = presence.get("sessionLoopState")
        party = presence.get("partyState")
        party_data = fresh.get("party", (0, {}))[1] or {}
        source = "none"
        candidate = None
        if core or loop == "INGAME":
            candidate, source = "IN_GAME", "core" if core else "presence"
        elif pre or loop == "PREGAME":
            candidate, source = "AGENT_SELECT", "pregame" if pre else "presence"
        elif loop == "MENUS" and party == "MATCHMADE_GAME_STARTING":
            candidate, source = "AGENT_SELECT", "presence_match_found"
        elif (loop == "MENUS" and party == "MATCHMAKING") or party_data.get("State") == "MATCHMAKING":
            candidate, source = "QUEUED", "presence" if party == "MATCHMAKING" else "party"
        elif loop == "MENUS" and (party in ("DEFAULT", None, "") or self.menu_samples >= 1 or self.presence_changed or now-self.lobby_verified_at <= .5):
            candidate, source = "MENUS", "presence"
        elif (now-self.lobby_verified_at <= .5 and
              all(k in fresh and fresh[k][1] is None for k in ("core", "pregame")) and
              party not in ("MATCHMAKING", "MATCHMADE_GAME_STARTING") and
              (self.state != "QUEUED" or party_data.get("State") == "DEFAULT")):
            candidate, source = "MENUS", "visual"

        # A negative lookup is absence only for that API. Require both APIs to
        # be absent plus menus presence before clearing a latched match phase.
        absent = all(k in fresh and fresh[k][1] is None and fresh[k][0] > self.transition_mono
                     for k in ("core", "pregame"))
        if self.state == "IN_GAME" and candidate != "IN_GAME":
            if not (candidate in ("MENUS", "QUEUED") and absent):
                candidate = None
        elif self.state == "AGENT_SELECT" and candidate in ("MENUS", "QUEUED"):
            if not absent:
                candidate = None
        if not self.identity:
            candidate, source = "OFFLINE", "process"
        if candidate:
            self._transition(candidate, source)

        if self.state == "QUEUED":
            raw = presence.get("queueEntryTime")
            queue_source = "presence"
            if timestamp(raw) is None:
                raw = (fresh.get("party", (0, {}))[1] or {}).get("QueueEntryTime")
                queue_source = "party"
            epoch = timestamp(raw)
            if epoch is not None and epoch <= time.time() + 2:
                if not self.queue_anchor or raw != self.queue_anchor[0]:
                    self.queue_anchor = (raw, now, max(0., time.time() - epoch), queue_source)
        else:
            self.queue_anchor = None

        evidence = fresh.get(source)
        if source == "presence_match_found":
            evidence = fresh.get("presence")
        # Unconfirmed startup presence is not a connectivity/freshness failure.
        # Fresh responses can legitimately leave us waiting for lobby evidence.
        # Degraded triggers if process loop has hung (>5s) or game is running but we have
        # zero API evidence across any stream within the freshness window.
        has_any_fresh = any(k in fresh for k in ("presence", "core", "pregame", "party", "names"))
        self.degraded = (now - self.process_at > 5.0 or
                         (bool(self.identity) and self.state != "LOADING" and not has_any_fresh))
        self.state_confirmed = candidate is not None
        self.source = source if candidate else "last_known"

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            self._reduce(now)
            visual = now - self.lobby_verified_at <= .5
            def current(key):
                entry = self.sources.get(key)
                return entry is not None and now-entry[0] <= self.freshness
            presence = self.sources.get("presence", (0, {}))[1] or {}
            api_ready = (self.menu_samples >= 2 and current("presence") and
                         presence.get("sessionLoopState") == "MENUS" and presence.get("partyState") == "DEFAULT" and
                         current("window") and self.sources["window"][1].get("exists", False) and
                         all(current(k) and self.sources[k][1] is None for k in ("core", "pregame")))
            ready = self.state == "MENUS" and self.state_confirmed and (api_ready or visual) and not self.degraded
            elapsed = None
            if self.queue_anchor and not self.degraded:
                elapsed = self.queue_anchor[2] + now - self.queue_anchor[1]
            allies = []
            roster_entry = self.sources.get("pregame_roster")
            names_entry = self.sources.get("names")
            current_pregame = self.sources.get("pregame", (0, None))[1] if current("pregame") else None
            if (self.state == "AGENT_SELECT" and roster_entry and current("pregame_roster") and
                    isinstance(roster_entry[1], dict) and roster_entry[1].get("match_id") == current_pregame):
                names = names_entry[1] if names_entry and current("names") and isinstance(names_entry[1], dict) else {}
                for player in roster_entry[1].get("players", []):
                    row = dict(player)
                    identity = names.get(row.pop("subject", ""), {})
                    row["name"] = identity.get("name") or ("You" if row.get("self") else "Teammate %d" % (row.get("slot", 0)+1))
                    row["tag"] = identity.get("tag") or ""
                    allies.append(row)
            party = {"available": False}
            party_entry = self.sources.get("party_roster")
            if self.identity and party_entry and now-party_entry[0] <= 8.:
                party = copy.deepcopy(party_entry[1])
                names = names_entry[1] if names_entry and current("names") else {}
                for row in party.get("members", []):
                    subject = row.pop("subject", "")
                    identity = names.get(subject, {}) if not row.pop("hidden", False) else {}
                    row["name"] = identity.get("name") or ("You" if row["self"] else "Party member")
                    row["tag"] = identity.get("tag", "")
                party["age_ms"] = round((now-party_entry[0])*1000)
            return {"ok": True, "party": party, "instance_id": self.instance, "generation": self.generation,
                    "sequence": self.seq, "state": self.state.lower(),
                    "phase": self.state, "state_since": self.since,
                    "game": bool(self.identity), "loading": self.state == "LOADING",
                    "ready_to_play": ready, "lobby_visual_verified": visual,
                    "readiness_basis": "visual" if ready and visual else "api" if ready else "unconfirmed",
                    "api_menu_candidate": self.state == "MENUS",
                    "degraded": self.degraded, "source": self.source,
                    "state_confirmed": self.state_confirmed,
                    "presence_loop": (self.sources.get("presence", (0, {}))[1] or {}).get("sessionLoopState"),
                    "presence_party": (self.sources.get("presence", (0, {}))[1] or {}).get("partyState"),
                    "presence_schema": (self.sources.get("presence", (0, {}))[1] or {}).get("schema"),
                    "visual_detection": copy.deepcopy(self.sources.get("visual", (0, {}))[1]),
                    "event_stream": copy.deepcopy(self.sources.get("events", (0, {}))[1]),
                    "queue_elapsed_secs": elapsed,
                    "queue_entry_time": self.queue_anchor[0] if self.queue_anchor else None,
                    "queue_clock": "clean_pc_wall_clock_anchored_to_monotonic",
                    "sampled_at": time.time(), "alert": copy.deepcopy(self.alert),
                    "pregame_id": (self.sources.get("pregame", (0, None))[1]
                                   if self.state == "AGENT_SELECT" else None),
                    "allies": allies,
                    "source_age_ms": {k: round((now-v[0])*1000) for k,v in self.sources.items()},
                    "errors": dict(self.errors)}



def sanitize_party(data, who):
    """Live social data only; no credentials, hidden identity, or disk cache."""
    if not isinstance(data, dict) or not isinstance(data.get("ID"), str):
        return {"available": False}
    members = []
    rows = data.get("Members")
    if not isinstance(rows, list):
        return {"available": False}
    for row in rows[:20]:
        if not isinstance(row, dict) or not isinstance(row.get("Subject"), str):
            continue
        identity = row.get("PlayerIdentity")
        identity = identity if isinstance(identity, dict) else {}
        members.append({"subject": row["Subject"], "self": row["Subject"] == who,
                        "leader": row.get("IsOwner") is True,
                        "ready": row.get("IsReady") is True,
                        "hidden": identity.get("Incognito") is True})
    if not any(row["self"] for row in members):
        return {"available": False}
    def strings(key):
        values = data.get(key)
        return [v[:96] for v in values[:32] if isinstance(v, str)] if isinstance(values, list) else []
    matchmaking = data.get("MatchmakingData")
    matchmaking = matchmaking if isinstance(matchmaking, dict) else {}
    return {"available": True, "id": data["ID"], "account_id": who,
            "state": str(data.get("State") or "UNKNOWN")[:48],
            "accessibility": str(data.get("Accessibility") or "UNKNOWN")[:16],
            "queue": str(matchmaking.get("QueueID") or "")[:48],
            "leader": any(row["self"] and row["leader"] for row in members),
            "members": members, "eligible_queues": strings("EligibleQueues"),
            "restrictions": strings("QueueIneligibilities"),
            "code": str(data.get("InviteCode") or "")[:32]}


def party_action(payload):
    """User-initiated commands, bound to the displayed game/account/party."""
    if not isinstance(payload, dict) or detector is None:
        return {"ok": False, "error": "Party unavailable"}
    if not detector.party_action_lock.acquire(blocking=False):
        return {"ok": False, "error": "A party action is already running"}
    try:
        snap = detector.store.snapshot()
        party = snap.get("party", {})
        if (snap["phase"] != "MENUS" or snap["degraded"] or not party.get("available") or
                any(payload.get(key) != value for key, value in
                    (("instance", snap["instance_id"]), ("generation", snap["generation"]),
                     ("party_id", party.get("id")), ("account_id", party.get("account_id"))))):
            return {"ok": False, "error": "Lobby changed. Wait for the party to refresh."}
        with detector.auth_lock:
            auth = detector.auth
        if not auth or auth[0] != party["account_id"] or auth[3] <= time.time():
            return {"ok": False, "error": "Account changed. Refresh the lobby."}
        def call(method, path, body=None):
            # Pin all requests to the same authentication snapshot.
            with detector.auth_lock:
                if detector.auth is not auth or time.monotonic() < detector.remote_pause_until:
                    raise ValueError("Authentication changed or rate limit cooldown active")
            base = f"https://glz-{detector.config.region}-1.{detector.config.shard}.a.pvp.net"
            with _HttpSession() as session:
                response = session.request(method, base+path, headers=auth[1], body=body, timeout=(1., 2.))
                if response.status_code == 401:
                    detector.refresh.set()
                if response.status_code == 429:
                    with detector.auth_lock:
                        detector.remote_pause_until = max(detector.remote_pause_until,
                            time.monotonic()+detector._retry_after(response.headers.get("Retry-After")))
                response.raise_for_status()
                return response.json() if response.body else {}
        quote = lambda value: urllib.parse.quote(value, safe="")
        player_path = "/parties/v1/players/"+quote(auth[0])
        current = call("GET", player_path)
        if current.get("CurrentPartyID") != party["id"]:
            return {"ok": False, "error": "Party changed. Try again after it refreshes."}
        path = "/parties/v1/parties/"+quote(party["id"])
        latest = sanitize_party(call("GET", path), auth[0])
        if not latest.get("available") or latest.get("state") != "DEFAULT":
            return {"ok": False, "error": "Party is no longer resting in the lobby."}
        action = payload.get("action")
        if action == "invite":
            name, tag = payload.get("name", ""), payload.get("tag", "")
            if (not isinstance(name, str) or not isinstance(tag, str) or not 1 <= len(name) <= 32 or
                    not 1 <= len(tag) <= 8 or any(ord(c) < 32 for c in name+tag)):
                return {"ok": False, "error": "Enter a valid Riot name and tag."}
            method, target = "POST", path+"/invites/name/"+quote(name)+"/tag/"+quote(tag)
        elif action == "join_code":
            code = payload.get("code", "")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{3,32}", code):
                return {"ok": False, "error": "Enter a valid party code."}
            method, target = "POST", "/parties/v1/players/joinbycode/"+quote(code)
        elif action in ("generate_code", "disable_code"):
            if not latest.get("leader"):
                return {"ok": False, "error": "Only the party leader can manage the code."}
            method, target = ("POST" if action == "generate_code" else "DELETE"), path+"/invitecode"
        elif action == "leave":
            method, target = "DELETE", player_path
        else:
            return {"ok": False, "error": "Unknown party action"}
        check = detector.store.snapshot()
        if check["generation"] != snap["generation"] or check["phase"] != "MENUS" or check["degraded"]:
            return {"ok": False, "error": "Game state changed. Action skipped."}
        call(method, target, {} if method == "POST" else None)
        # Hide the old party until the existing worker confirms the new state.
        detector.store.observe("party_roster", {"available": False}, snap["generation"])
        detector.remote_wake["party"].set()
        return {"ok": True, "message": "Party action accepted. Refreshing lobby."}
    except (_NetworkError, ValueError, KeyError, TypeError):
        return {"ok": False, "error": "Riot did not confirm the party action. Check the client before retrying."}
    finally:
        detector.party_action_lock.release()


class Detector:
    def __init__(self, config):
        self.config = config
        self.store = StateStore(config.freshness)
        self.stop_event = threading.Event()
        self.threads = []
        self.auth_lock = threading.Lock()
        self.party_action_lock = threading.Lock()
        self.auth = None
        self.window_cache = {"exists": False}
        self.refresh = threading.Event()
        self.remote_pause_until = 0.
        self.dashboard_lock = threading.Lock()
        self.dashboard_data = {}
        self.dashboard_subject = None
        self.remote_wake = {kind: threading.Event() for kind in ("core", "pregame", "party")}
        self.lockfile = Path(os.environ.get("LOCALAPPDATA", "")) / "Riot Games/Riot Client/Config/lockfile"

    def start(self):
        self._spawn(self._dashboard_loop)
        for target in (self._process_loop, self._local_loop, self._visual_loop, self._event_loop):
            self._spawn(target)
        for kind in ("core", "pregame", "party"):
            self._spawn(lambda k=kind: self._remote_loop(k))
        return self

    def _spawn(self, target):
        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()

    def dashboard_snapshot(self):
        with self.auth_lock:
            auth = self.auth
        with self.dashboard_lock:
            if not auth or auth[3] <= time.time() or auth[0] != self.dashboard_subject:
                return {"available": False}
            result = copy.deepcopy(self.dashboard_data)
        updated_at = result.pop("fetched_at", 0)
        age = time.time()-updated_at
        if result:
            result['account_id'] = auth[0]
            result['updated_at'] = updated_at
        result["available"] = bool(result) and age < 90
        result["age_secs"] = max(0, round(age)) if result["available"] else None
        return result if result["available"] else {"available": False}

    def _dashboard_loop(self):
        """Read-only account data; independent from latency-sensitive state workers."""
        currency_ids = {"vp": "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741",
                        "rp": "e59aa87c-4cbf-517a-5983-6e81511be9b7",
                        "kc": "85ca954a-41f2-ce94-9b45-8ca3dd39a00d"}
        due = 0
        last_subject = None
        with self._session() as session:
            while not self.stop_event.wait(.5):
                with self.auth_lock:
                    auth = self.auth
                    pause = self.remote_pause_until
                if not auth or auth[3] <= time.time() or time.monotonic() < pause:
                    continue
                who, headers, _, _ = auth
                if who != last_subject:
                    due, last_subject = 0, who
                    with self.dashboard_lock:
                        self.dashboard_data = {}
                        self.dashboard_subject = who
                if time.monotonic() < due:
                    continue
                due = time.monotonic()+30
                base = f"https://pd.{self.config.shard}.a.pvp.net"
                result = {"fetched_at": time.time(), "balances": {}, "owned_agents": [], "ownership_available": False}
                for kind, path in (("wallet", f"/store/v1/wallet/{who}"),
                                   ("agents", f"/store/v1/entitlements/{who}/01bb38e1-da47-4e6a-9b3d-945fe4655707")):
                    try:
                        with session.get(base+path, headers=headers, timeout=(.5,1.5)) as response:
                            if response.status_code == 401:
                                self.refresh.set()
                            if response.status_code == 429:
                                with self.auth_lock:
                                    self.remote_pause_until = max(self.remote_pause_until, time.monotonic()+self._retry_after(response.headers.get("Retry-After")))
                                break
                            response.raise_for_status()
                            data = response.json()
                        if kind == "wallet":
                            balances = data.get("Balances", {})
                            result["balances"] = {name: balances[uid] for name, uid in currency_ids.items()
                                                  if type(balances.get(uid)) is int and balances[uid] >= 0}
                        else:
                            result["owned_agents"] = [row["ItemID"] for row in data.get("Entitlements", [])
                                                      if isinstance(row, dict) and isinstance(row.get("ItemID"), str)][:100]
                            result["ownership_available"] = isinstance(data.get("Entitlements"), list)
                    except (_NetworkError, ValueError, KeyError, TypeError):
                        pass
                with self.auth_lock:
                    current_auth = self.auth
                if current_auth is auth:
                    with self.dashboard_lock:
                        self.dashboard_subject = who
                        self.dashboard_data = result

    def close(self):
        self.stop_event.set()
        for wake in self.remote_wake.values():
            wake.set()
        for thread in self.threads:
            thread.join(timeout=3)

    def _process_loop(self):
        while not self.stop_event.is_set():
            try:
                self.store.process(_shipping_processes())
                self.window_cache = get_window_info()
                self.store.observe("window", self.window_cache, self.store.generation)
            except OSError:
                self.store.touch_process()
                self.store.error("process", "process_inspection_failed")
            self.stop_event.wait(.15)

    def _visual_loop(self):
        hits = 0
        previous_generation = None
        # Thread-local DPI awareness keeps client coordinates and pixels aligned.
        try:
            user = ctypes.WinDLL("user32")
            user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
        while not self.stop_event.is_set():
            began = time.monotonic()
            with self.store.lock:
                generation, identity = self.store.generation, self.store.identity
            if generation != previous_generation:
                hits = 0
                previous_generation = generation
            try:
                pixels = capture_play_banner(identity)
                score = play_banner_score(pixels) if pixels else 0.
                hits = min(2, hits+1) if score >= .76 else 0
                if hits >= 2:
                    self.store.verify_lobby(generation)
                elif identity:
                    with self.store.lock:
                        if generation == self.store.generation:
                            self.store.lobby_verified_at = 0.
                self.store.observe("visual", {"play_score": round(score, 3), "confirmed": hits >= 2,
                                               "method": "PrintWindow",
                                               "capture_available": pixels is not None}, generation, began)
            except (OSError, ValueError, ctypes.ArgumentError):
                hits = 0
                self.store.error("visual", "screen_capture_failed")
            self.stop_event.wait(max(.01, .2-(time.monotonic()-began)))

    @staticmethod
    def _session():
        session = _HttpSession()
        session.trust_env = False
        return session

    def _handle_event(self, message, subject, generation):
        event = json.loads(message)
        if not isinstance(event, list) or len(event) < 3 or event[0] != 8 or not isinstance(event[2], dict):
            return
        body = event[2]
        uri = body.get("uri", "")
        if uri == "/chat/v4/presences" and body.get("eventType") != "Delete":
            data = body.get("data") or {}
            rows = data.get("presences", []) if isinstance(data, dict) else data
            for row in rows:
                if row.get("puuid") == subject and row.get("product") == "valorant" and row.get("private"):
                    presence = normalize_presence(json.loads(base64.b64decode(row["private"])))
                    self.store.observe("presence", presence, generation)
                    loop = presence["sessionLoopState"]
                    self.remote_wake["core" if loop == "INGAME" else "pregame"].set()
                    break
        elif isinstance(uri, str) and uri.startswith("/riot-messaging-service/v1/message/"):
            # RMS announces a changed resource, not necessarily a entered match.
            # Wake the authoritative player lookup; never turn Delete into IN_GAME.
            if "ares-core-game" in uri or "ares-coregame" in uri:
                self.remote_wake["core"].set()
            elif "ares-pregame" in uri:
                self.remote_wake["pregame"].set()
            elif "ares-parties" in uri:
                self.remote_wake["party"].set()

    def _event_loop(self):
        failures = 0
        while not self.stop_event.is_set():
            connection = None
            generation = self.store.generation
            try:
                with self.auth_lock:
                    auth = self.auth
                if not auth or not self.store.identity:
                    self.stop_event.wait(.25)
                    continue
                raw = self.lockfile.read_text().strip()
                _, _, port, password, _ = raw.split(":")
                generation, subject = self.store.generation, auth[0]
                connection = RiotEvents(port, password)
                for name in ("OnJsonApiEvent_chat_v4_presences", "OnJsonApiEvent_riot-messaging-service_v1_message"):
                    connection.send(json.dumps([5, name]))
                failures = 0
                count = 0
                while not self.stop_event.is_set():
                    if self.store.generation != generation or self.lockfile.read_text().strip() != raw:
                        break
                    message = connection.receive()
                    if message is not None:
                        try:
                            self._handle_event(message, subject, generation)
                            count += 1
                        except (ValueError, TypeError, AttributeError, KeyError):
                            self.store.error("events", "unrecognized_event_payload")
                    self.store.observe("events", {"connected": True, "messages": count}, generation)
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                failures += 1
                self.store.error("events", "event_stream_unavailable_rest_polling_active")
            finally:
                if connection:
                    connection.close()
                    self.store.observe("events", {"connected": False}, generation)
                    if failures:
                        self.store.error("events", "event_stream_unavailable_rest_polling_active")
            self.stop_event.wait(min(10., .5*2**min(failures, 4)))

    def _local_loop(self):
        fingerprint = None
        subject = None
        retry_at = 0.
        failures = 0
        with self._session() as session:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    raw = self.lockfile.read_text().strip()
                    name, pid, port, password, protocol = raw.split(":")
                    if protocol not in ("https", "http") or not 0 < int(port) < 65536:
                        raise ValueError("invalid_lockfile")
                    if raw != fingerprint:
                        self.store.invalidate_session()
                        with self.auth_lock:
                            self.auth = None
                        fingerprint, subject, retry_at = raw, None, 0.
                    url = f"{protocol}://127.0.0.1:{int(port)}"
                    session.auth = ("riot", password)
                    generation = self.store.generation
                    with self.auth_lock:
                        auth = self.auth
                    if started >= retry_at and (not auth or auth[3] < time.time()+60 or self.refresh.is_set()):
                        retry_at = started + 5  # coalesce concurrent 401s
                        self.refresh.clear()
                        with session.get(url+"/entitlements/v1/token", verify=False, timeout=(.3,.4)) as r:
                            r.raise_for_status()
                            tokens = r.json()
                        who = tokens["subject"]
                        if subject is not None and who != subject:
                            self.store.invalidate_session()
                            generation = self.store.generation
                        subject = who
                        headers = {"Authorization": "Bearer "+tokens["accessToken"],
                                   "X-Riot-Entitlements-JWT": tokens["token"],
                                   "X-Riot-ClientVersion": self.config.client_version,
                                   "X-Riot-ClientPlatform": base64.b64encode(json.dumps({
                                       "platformType":"PC", "platformOS":"Windows",
                                       "platformOSVersion":"10.0.19045.1.256.64bit",
                                       "platformChipset":"Unknown"}).encode()).decode()}
                        with self.auth_lock:
                            self.auth = (who, headers, generation,
                                         min(jwt_exp(tokens["accessToken"]), jwt_exp(tokens["token"])))
                    if subject:
                        with session.get(url+"/chat/v4/presences", verify=False, timeout=(.3,.4)) as r:
                            r.raise_for_status()
                            data = r.json()
                        rows = data.get("presences", []) if isinstance(data, dict) else data
                        names = {}
                        for presence in rows if isinstance(rows, list) else []:
                            if not isinstance(presence, dict) or presence.get("product") != "valorant":
                                continue
                            player_id = presence.get("puuid")
                            if not isinstance(player_id, str) or not player_id:
                                continue
                            game_name = presence.get("game_name") or presence.get("gameName") or presence.get("name") or ""
                            game_tag = presence.get("game_tag") or presence.get("gameTag") or ""
                            names[player_id] = {"name": str(game_name)[:32], "tag": str(game_tag)[:8]}
                        self.store.observe("names", names, generation, started)
                        own = next((p for p in rows if p.get("puuid") == subject and
                                    p.get("product") == "valorant"), None)
                        if own:
                            private = normalize_presence(json.loads(base64.b64decode(own["private"])))
                            self.store.observe("presence", private, generation, started)
                        else:
                            self.store.error("presence", "self_presence_missing")
                    failures = 0
                except (OSError, ValueError, KeyError, TypeError, _NetworkError):
                    failures += 1
                    self.store.error("presence", "local_api_unavailable_or_schema_changed")
                    if not self.lockfile.exists():
                        with self.auth_lock:
                            self.auth = None
                        if fingerprint is not None:
                            self.store.invalidate_session()
                        fingerprint, subject = None, None
                delay = min(5., .25 * 2**min(failures, 5)) if failures else self.config.local_interval
                self.stop_event.wait(max(.01, delay - (time.monotonic()-started)))

    def _remote_loop(self, kind):
        base = f"https://glz-{self.config.region}-1.{self.config.shard}.a.pvp.net"
        failures = 0
        with self._session() as session:
            while not self.stop_event.is_set():
                began = time.monotonic()
                self.remote_wake[kind].clear()
                with self.auth_lock:
                    auth = self.auth
                    pause = self.remote_pause_until
                if not auth or not self.store.identity or began < pause or auth[3] <= time.time():
                    self.stop_event.wait(.1)
                    continue
                who, headers, _, expiry = auth
                generation = self.store.generation
                prefix = {"core":"core-game", "pregame":"pregame", "party":"parties"}[kind]
                path = f"/{prefix}/v1/players/{who}"
                try:
                    with session.get(base+path, headers=headers, timeout=(.5,.7)) as r:
                        if r.status_code == 429:
                            delay = self._retry_after(r.headers.get("Retry-After"))
                            with self.auth_lock:
                                self.remote_pause_until = max(self.remote_pause_until, time.monotonic()+delay)
                            raise ValueError("rate_limited")
                        if r.status_code == 401:
                            self.refresh.set()
                        if r.status_code == 404:
                            value = None if kind != "party" else {}
                        else:
                            r.raise_for_status()
                            data = r.json()
                            key = "CurrentPartyID" if kind == "party" else "MatchID"
                            value = data[key]
                            if not isinstance(value, str) or not value:
                                raise ValueError("missing_id")
                    if kind == "party" and not value:
                        self.store.observe("party_roster", {"available": False}, generation, began)
                    if kind == "party" and value:
                        with session.get(base+f"/parties/v1/parties/{value}", headers=headers,
                                         timeout=(.5,.7)) as r:
                            if r.status_code == 401:
                                self.refresh.set()
                            if r.status_code == 429:
                                with self.auth_lock:
                                    self.remote_pause_until = max(self.remote_pause_until,
                                        time.monotonic()+self._retry_after(r.headers.get("Retry-After")))
                            r.raise_for_status()
                            party_response = r.json()
                            roster = sanitize_party(party_response, who)
                            roster["pending_requests"] = len(data.get("Requests") or [])
                            with self.auth_lock:
                                same_account = self.auth is auth
                            if same_account:
                                self.store.observe("party_roster", roster, generation, began)
                            value = dict(party_response.get("MatchmakingData") or {})
                            value["State"] = party_response.get("State")
                    elif kind == "pregame" and value:
                        try:
                            with session.get(base+f"/pregame/v1/matches/{value}", headers=headers,
                                             timeout=(.5,.7)) as r:
                                if r.status_code == 401:
                                    self.refresh.set()
                                if r.status_code == 429:
                                    with self.auth_lock:
                                        self.remote_pause_until = max(self.remote_pause_until,
                                            time.monotonic()+self._retry_after(r.headers.get("Retry-After")))
                                r.raise_for_status()
                                roster = {"match_id": value,
                                          "players": sanitize_pregame_roster(r.json(), who)}
                            self.store.observe("pregame_roster", roster, generation, began)
                        except (_NetworkError, ValueError, KeyError, TypeError):
                            self.store.error("pregame_roster", "roster_unavailable")
                    with self.auth_lock:
                        current_auth = self.auth
                    if current_auth is auth:
                        self.store.observe(kind, value, generation, began)
                    failures = 0
                except (_NetworkError, ValueError, KeyError, TypeError):
                    failures += 1
                    self.store.error(kind, "request_failed_check_auth_region_version_or_rate_limit")
                state = self.store.snapshot()["phase"]
                interval = (self.config.transition_interval if state in
                            ("QUEUED", "AGENT_SELECT", "LOADING") else self.config.idle_interval)
                if kind == "party":
                    interval = max(interval, 3.)
                if failures:
                    interval = max(interval, min(30., 2**min(failures, 5)) + random.random())
                # Events shorten an idle poll delay, but never bypass failure backoff
                # or allow requests closer than the configured transition interval.
                if failures:
                    self.stop_event.wait(max(.01, interval-(time.monotonic()-began)))
                else:
                    self.stop_event.wait(max(0., self.config.transition_interval-(time.monotonic()-began)))
                    self.remote_wake[kind].wait(max(0., interval-(time.monotonic()-began)))

    @staticmethod
    def _retry_after(value):
        try:
            return max(1., float(value))
        except (ValueError, TypeError):
            try:
                return max(1., email.utils.parsedate_to_datetime(value).timestamp()-time.time())
            except (ValueError, TypeError, AttributeError):
                return 10.



detector = None

# ── HTTP REQUEST HANDLER ─────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def _send(self, obj):
        try:
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:
            pass

    def do_POST(self):
        if self.path != "/party/action":
            self._send({"ok": False, "error": "Unknown endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Origin") or not 0 < length <= 4096 or self.headers.get_content_type() != "application/json":
                raise ValueError("Invalid party request")
            self._send(party_action(json.loads(self.rfile.read(length))))
        except (ValueError, TypeError):
            self._send({"ok": False, "error": "Invalid party request"})

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/status", "/state"):
                status = detector.store.snapshot()
                status["dashboard"] = detector.dashboard_snapshot()
                status["queue_modes"] = [{"id": key, "name": name} for key, name in QUEUE_MODES.items()]
                status["agent_catalog"] = [{"name": name, "id": uid} for name, uid in AGENTS.items()]
                status["launch_status"] = get_launch_status()
                status["window"] = dict(detector.window_cache)
                status["map"] = "Unknown"
                status["last_state_line"] = status["source"]
                status["loading_stage"] = "Waiting for current lobby evidence" if status["loading"] else ""
                self._send(status)
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
                    "riot_client_available": bool(lf),
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
    global detector
    # Existing region discovery remains the default; explicit overrides are optional.
    region = os.environ.get("VAL_REGION") or region_of()[0]
    shard = os.environ.get("VAL_SHARD") or {"br": "na", "latam": "na"}.get(region, region)
    version = os.environ.get("VAL_CLIENT_VERSION") or CLIENT_VERSION
    detector = Detector(Config(region=region, shard=shard, client_version=version)).start()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("clean_agent listening on 0.0.0.0:%d" % PORT)
    print("Single file / standard library / cached status detector active")
    print("Region=%s shard=%s client_version=%s" % (region, shard, version))
    print("Lobby readiness: fresh repeated MENUS presence + game window + no pregame/core match")
    print("endpoints: /status /window/focus /launch /kill /log /queue /cancel /pick")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        detector.close()

if __name__ == "__main__":
    main()
