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
- **Multi-Source Hybrid Game State Engine**:
  - Combines Win32 window metrics, log file tailing (`ShooterGame.log`), and game local/GLZ APIs.
  - Automatic map name resolution (`ASCENT`, `BIND`, `HAVEN`, `SPLIT`, `ABYSS`, `SUNSET`, etc.).
  - Full agent roster support with up-to-date UUIDs.
- **Zero External Dependencies**:
  - Uses only Python standard library + built-in `ctypes`.
  - No `pip install` required.
  - No administrative privileges or background persistence needed.

## Setup & Running

1. Ensure Python 3.8+ is installed on the clean PC.
2. Run directly via terminal or double-click `start.bat`:
   ```bash
   python clean_agent.py
   ```
3. The server starts listening on `0.0.0.0:8090`.

## HTTP API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/status` | Returns game process state, window metrics, state, active map, and match IDs |
| `GET` | `/window/focus` | Restores the VALORANT window from minimized state and brings it to foreground |
| `GET` | `/queue?mode=<mode>` | Starts matchmaking queue (`unrated`, `competitive`, `swiftplay`, etc.) |
| `GET` | `/cancel` | Cancels current matchmaking queue |
| `GET` | `/pick?agent=<name>` | Selects and locks the specified agent during pregame |
| `GET` | `/launch` | Launches VALORANT via Riot Client API |
| `GET` | `/kill` | Closes VALORANT and Riot Client processes |
| `GET` | `/log?n=50` | Returns the last `n` state-relevant log lines |
| `GET` | `/session` | Returns local lockfile, region, and window state |

## License

MIT
