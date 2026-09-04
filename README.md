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

`ready_to_play` deliberately requires current visual lobby confirmation via
`detector.store.verify_lobby(generation)`. No screen recognizer is bundled, so it
remains false by default. `api_menu_candidate` reports backend menu evidence
separately. The existing GUI must not override readiness merely because state is
`menus`. This preserves the strict readiness contract rather than claiming that
backend presence proves the lobby has rendered.

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
