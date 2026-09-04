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
    (re.compile(r"main/lobby|LogUINavigationModel", re.I),             "menus",        10),
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

def detect_state():
    if not game_running():
        return "menus", "?", "game not running"
    for line in reversed(tail_lines()):
        for rx, name, pri in MARKERS:
            if rx.search(line):
                m = re.search(r"\[(\d{4}\.\d{2}\.\d{2})-(\d{2}:\d{2}:\d{2}:\d{3})\]?", line)
                return name, m.group(0) if m else "?", line.strip()[:200]
    return "menus", "?", ""

def wait_lockfile(seconds):
    for _ in range(seconds * 2):
        lf = read_lockfile()
        if lf:
            return lf
        time.sleep(0.5)
    return None

def rc_trigger_launch(lf):
    """Trigger Valorant launch via Riot Client's internal Foundation API."""
    try:
        url = "https://127.0.0.1:%d/product-launcher/v1/products/valorant/patchlines/live" % lf["port"]
        auth = base64.b64encode(("riot:%s" % lf["password"]).encode()).decode()
        req = urllib.request.Request(url, data=b"{}", method="POST",
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=_CTX, timeout=10) as r:
            body = r.read().decode().strip()
            print("Valorant launched via Riot Client API: %s" % body)
            return True, "Valorant is starting (Riot Client API session %s)" % body
    except Exception as e:
        print("Riot Client API launch call failed: %s" % e)
        return False, str(e)

def launch():
    if game_running():
        return ["already-running"], None

    # Check candidate paths for RiotClientServices.exe
    riot_client_path = r"C:\Riot Games\Riot Client\RiotClientServices.exe"
    candidates = [
        riot_client_path,
        r"C:\Program Files\Riot Games\Riot Client\RiotClientServices.exe",
        r"C:\Program Files (x86)\Riot Games\Riot Client\RiotClientServices.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Riot Games\Riot Client\RiotClientServices.exe"),
    ]
    rc_detected = find_rc_exe()
    if rc_detected and rc_detected not in candidates:
        candidates.insert(0, rc_detected)

    chosen_rc = None
    for p in candidates:
        if p and os.path.exists(p):
            chosen_rc = p
            break

    # 1. If Riot Client lockfile is already active, trigger product-launcher API immediately
    lf = read_lockfile()
    if lf:
        ok, msg = rc_trigger_launch(lf)
        if ok:
            return [msg], None

    # 2. If Riot Client is not active or lockfile was absent, launch Riot Client process
    if chosen_rc:
        try:
            cwd = os.path.dirname(chosen_rc)
            subprocess.Popen([chosen_rc, "--launch-product=valorant", "--launch-patchline=live"], cwd=cwd)
            print("Riot Client spawned: %s" % chosen_rc)
        except Exception as e:
            print("Failed spawning Riot Client: %s" % e)

        # Wait for Riot Client to initialize and create lockfile (up to 15s)
        lf = wait_lockfile(15)
        if lf:
            # Send the Foundation launch request to guarantee the game starts
            ok, msg = rc_trigger_launch(lf)
            if ok:
                return [msg], None

        return ["Valorant launch command sent to Riot Client"], None

    # Fallback to direct game exe if Riot Client not found
    game = find_game_exe()
    if game:
        try:
            subprocess.Popen([game], cwd=os.path.dirname(game))
            return ["Valorant is starting (direct exe)..."], None
        except Exception as e:
            return None, "direct launch blocked: %s" % e

    return None, "Riot Client path not found. Please check the installation path."

def kill_game():
    subprocess.run(["taskkill", "/F", "/IM", GAME_EXE], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "VALORANT.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "RiotClientServices.exe"], capture_output=True)

# ── RIOT LOCAL + GLZ API ─────────────────────────────────────────────
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

def read_lockfile():
    try:
        with open(LOCKFILE, "r") as f:
            parts = f.read().strip().split(":")
        if len(parts) >= 5:
            return {"port": int(parts[1]), "password": parts[3], "protocol": parts[4]}
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

CLIENT_VERSION = "release-13.04-shipping-20-5340415"
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

def party_id():
    data, err = glz("GET", "/parties/v1/parties")
    if err:
        return None, err
    return data.get("ID") or data.get("id"), None

def start_queue(mode):
    pid, err = party_id()
    if err or not pid:
        return {"ok": False, "error": err or "no party"}
    data, err = glz("POST", "/parties/v1/parties/%s/queue" % pid, {"queueID": mode})
    if err and "HTTP 400" in err:
        return {"ok": False, "error": err, "party": pid}
    return {"ok": err is None, "party": pid, "queue": mode, "resp": data, "error": err}

def cancel_queue():
    pid, err = party_id()
    if err or not pid:
        return {"ok": False, "error": err or "no party"}
    data, err = glz("DELETE", "/parties/v1/parties/%s/queue" % pid)
    return {"ok": err is None, "party": pid, "resp": data, "error": err}

def pick_agent(name):
    agent_id = AGENTS.get(name)
    if not agent_id:
        return {"ok": False, "error": "unknown agent: %s" % name}
    data, err = glz("GET", "/pregame/v1/players/me")
    if err:
        return {"ok": False, "error": err}
    mid = data.get("MatchID") or data.get("matchId")
    if not mid:
        return {"ok": False, "error": "not in a pregame match"}
    sel, err1 = glz("POST", "/pregame/v1/matches/%s/select" % mid, {"agentId": agent_id})
    lock, err2 = glz("POST", "/pregame/v1/matches/%s/lock" % mid, {})
    return {"ok": err1 is None and err2 is None, "match": mid, "agent": name,
            "select_err": err1, "lock_err": err2}

# ── HTTP REQUEST HANDLER ─────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/status", "/state"):
                win = get_window_info()
                running = game_running() or win["exists"]

                map_name = "Unknown"
                pregame_mid = None
                if running:
                    try:
                        p_me, _ = glz("GET", "/pregame/v1/players/me")
                        if p_me and (p_me.get("MatchID") or p_me.get("matchId")):
                            pregame_mid = p_me.get("MatchID") or p_me.get("matchId")
                            match_data, _ = glz("GET", "/pregame/v1/matches/%s" % pregame_mid)
                            if match_data:
                                map_name = resolve_map_name(match_data.get("MapID", ""))
                    except Exception:
                        pass

                if pregame_mid:
                    state = "agent_select"
                    since = "live-pregame"
                    line = "Pregame match active: " + str(pregame_mid)
                else:
                    state, since, line = detect_state()

                self._send({
                    "ok": True,
                    "game": running,
                    "window": win,
                    "state": state,
                    "state_since": since,
                    "last_state_line": line,
                    "map": map_name,
                    "pregame_id": pregame_mid
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
