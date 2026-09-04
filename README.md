# RemoteVal

Lightweight remote companion agent for 2PC VALORANT setups. Runs on the clean/secondary PC to monitor game status, inspect the VALORANT window, control matchmaking queues, and manage agent selections remotely over local network HTTP.

## Features

- **Win32 Window Inspection (Pure ctypes)**:
  - Real-time window handle (`hwnd`) detection.
  - Client resolution & screen bounds tracking (`width`, `height`, `rect`).
  - Minimized state and foreground focus monitoring.
  - Window restore and foreground focus management (`/window/focus`).
- **Game Matchmaking & Agent Controls**:
  - Start queue with specified game mode (`unrated`, `competitive`, `swiftplay`, `spikerush`, `deathmatch`, etc.).
  - Cancel active queue instantly.
  - Remote agent select & lock-in during pregame (`/pick?agent=Jett`).
- **Cached Game State Engine**:
  - Independent native process, local presence/auth, pregame, core-game, and party workers.
  - `/status` reads an in-memory snapshot; no curl subprocesses or upstream requests.
  - Core-game takes precedence over pregame, which takes precedence over queueing.
  - Queue time comes from Riot's queue timestamp; transition alerts remain available across missed polls.
  - Authentication is cached from the running Riot Client, with expiration and rate-limit handling.
  - Logs remain diagnostic only. The status map is currently `Unknown`.
- **Zero External Dependencies**:
  - Uses only Python standard library + built-in `ctypes`.
  - No `pip install` required.
  - No administrative privileges or background persistence needed.

## Setup & Running

1. Ensure Python 3.10+ is installed on the clean PC.
2. Run directly via terminal or double-click `start.bat`:
   ```bash
   python clean_agent.py
   ```
3. The server starts listening on `0.0.0.0:8090`.

To update an existing checkout, stop the running agent with Ctrl+C, then run:

```powershell
git pull --ff-only
py clean_agent.py
```

Only `clean_agent.py` is required at runtime. There is no detector module to copy,
no virtual environment requirement, and no pip installation step. Tests are for
development only.

The agent retains the existing region discovery and bundled client-version default.
If they do not match your installation, `VAL_REGION`, `VAL_SHARD`, and
`VAL_CLIENT_VERSION` can override them. Startup prints the selected values; the
bundled client version is not automatically updated when Riot releases a patch.

### Detection limits and GUI contract

Core-game membership is a backend signal, not proof of the exact rendered 5v5
screen. `alert.kind=CORE_GAME_ENTERED` includes loading and active gameplay;
deduplicate notifications using `(instance_id, alert.id)`. No hard sub-second
visual guarantee is possible from these APIs. The default transition polling
interval is 500 ms per remote worker, subject to RTT and backoff.

`ready_to_play` uses automatic API readiness: two fresh own-player MENUS/DEFAULT
presence samples, a detected game window, and fresh negative pregame and core-game
lookups. Missing or expired evidence disables PLAY. Process/session changes reset
the evidence. `readiness_basis=api` explicitly distinguishes this from visual proof;
`lobby_visual_verified` distinguishes the embedded visual fallback from API readiness.
The GUI must honor `ready_to_play`, not enable PLAY from `menus` alone. A fresh but
unconfirmed startup state is no longer incorrectly marked as stale.

An embedded visual fallback now recognizes the white **PLAY** text on the red
top-center banner from the supplied English main-menu screenshot. It uses native
Windows GDI capture and a compressed glyph template inside `clean_agent.py`, with
no OCR installation or image files needed at runtime. Two consecutive matches at
200 ms intervals confirm the banner. The score is exposed as
`visual_detection.play_score`. Capture uses the actual game's process-owned
`UnrealWindow` HWND with `PrintWindow`, not desktop pixels, so another app covering
the game is not included. `capture_available=false` means there is no suitable
window, it is minimized, its aspect ratio is unsupported, or capture failed/was blank.

The recognizer supports the supplied 16:9 layout, including 1366x768 and 1920x1080.
Different languages, aspect ratios, UI layouts, or GPU/exclusive-fullscreen capture
behavior may prevent a match. PrintWindow support depends on the rendering backend;
if it supplies a blank frame, there is deliberately no desktop-capture fallback.
With missing presence fields, it requires fresh negative pregame/core-game lookups
before enabling PLAY. Positive queue/match evidence wins. Visual evidence expires
after 500 ms and never turns a core-game transition into a claimed 5v5 screenshot.

Stop the queue animation whenever phase is not `QUEUED`, status is `degraded`, or
communication goes stale. Anchor numeric `queue_elapsed_secs` to the GUI's steady
clock; do not reset it on entering queue. A null duration means no current reliable
timestamp. Poll at 100–200 ms during transitions for low additional GUI latency.
The existing GUI requires these consumption changes separately.

The command endpoints remain LAN remote-control endpoints. Restrict access to the
intended Gaming PC. `/session` no longer includes the lockfile password.

Run the offline tests with `py -m unittest discover -p "test_*.py" -v`.

## HTTP API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/status` | Cached process/window state, phase, freshness, queue duration, and retained transition alert |
| `GET` | `/window/focus` | Restores the VALORANT window from minimized state and brings it to foreground |
| `GET` | `/queue?mode=<mode>` | Starts matchmaking queue (`unrated`, `competitive`, `swiftplay`, etc.) |
| `GET` | `/cancel` | Cancels current matchmaking queue |
| `GET` | `/pick?agent=<name>` | Selects and locks the specified agent during pregame |
| `GET` | `/launch` | Launches VALORANT via Riot Client API |
| `GET` | `/kill` | Closes VALORANT and Riot Client processes |
| `GET` | `/log?n=50` | Returns the last `n` state-relevant log lines |
| `GET` | `/session` | Returns Riot Client availability, region, and window state (no credentials) |

## License

MIT
