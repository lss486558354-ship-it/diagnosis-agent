# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

3D printer automation controller that drives BambuLab Studio via mouse/keyboard recording playback, pixel-based UI state detection, and a PLC-style state machine. Targets Ubuntu 22.04 + Xorg; also runs on Windows for development and recording authoring.

## Commands

```bash
# Install dependencies (Ubuntu)
sudo apt install -y python3 python3-pip python3-tk python3-pil xdotool
pip install pynput pillow pyserial

# Run headless controller (production)
python3 main.py

# Run GUI tool (recording authoring, pixel monitor, state machine editor)
python3 mouse_recorder.py

# Check that all modules import correctly
python3 -c "
from mouse_recorder import IS_LINUX, PYNPUT_OK
from pixel_monitor import _capture_region_rgb
from state_machine import StateMachineEngine
from cloud_service import CloudService
print('All OK')
"

# Verify Xorg session (required for xdotool and PIL screenshots)
echo $XDG_SESSION_TYPE   # must output "x11"
```

## Architecture

### Two Entry Points

| Entry | Class | Purpose |
|-------|-------|---------|
| `main.py` | `PrinterController` | Headless daemon: startup → cloud connect → idle loop → dispatch tasks |
| `mouse_recorder.py` | `MouseRecorderApp` (tkinter) | GUI with 5 tabs: Record/Replay, Pixel Monitor, State Machine, Cloud Config, Help |

### Startup Sequence (main.py)

```
_init_components()          # Wire all callbacks between modules
  → _launch_studio()        # Replay "launch" recording → pixel-confirm Studio is visible
  → _connect_cloud()        # POST /ready → start heartbeat (30s) → send "idle" status
  → _run_main_loop()        # Sleep loop, cloud messages arrive via callback
```

### Thread Model

All background services run as **daemon threads**, created on demand:

| Thread name | Created by | Role |
|-------------|-----------|------|
| `cloud-service` | `CloudService.start()` | Polls `GET /api/v1/commands` every N seconds |
| `heartbeat` | `HeartbeatService.start()` | Posts heartbeat payload every 30s |
| `print-timer` (per task) | `PrintManager.submit()` | Monitors print progress, timeout, cancel |
| `sm-engine` (per instance) | `StateMachineEngine.start()` | Polls pixel conditions → state transitions |
| `region-monitor` | `PixelMonitor` | Screenshots → hash comparison → rule triggers |

All callbacks fire on the **calling thread** (not the main thread). `PrinterController` callbacks are thread-safe via a `threading.Lock`.

### Callback Wiring Pattern

Components use setter-based dependency injection. Example from `PrinterController._init_components()`:

```python
self.cloud.set_message_callback(self._on_cloud_message)
self.print_mgr.set_replay_callback(self._replay_recording)
self.print_mgr.set_pixel_monitor(self.pixel_monitor)
```

### State Machine System

Two layers of rules, evaluated in order (first match wins):

1. **Jump Rules** (`jump_rules/` directory, one JSON file per rule) — shared across states via `state.jump_rules` (list of rule names). Each rule has its own conditions and target state.
2. **Transitions** (inline in `state_machine.json`) — per-state `transitions` list, each with conditions + target.

Condition evaluation uses `AND` logic across conditions within a rule/transition. Conditions support two modes:
- `match` — level-triggered: color is present → condition true
- `changed` — edge-triggered: color transitions from absent→present → condition true

### Pixel Monitor

Platform-adaptive screenshot engine:
- **Windows**: GDI `BitBlt` → BGRA→RGB conversion
- **Linux**: PIL `ImageGrab`

Core function: `_capture_region_rgb(x1, y1, x2, y2) → bytes` (R,G,B,R,G,B,...). Color matching via `_scan_rgb_for_color(data, r, g, b, tolerance)` with Euclidean distance in RGB space.

### OTA Update System

Four update types dispatched by cloud message type:

| Type | What it replaces |
|------|-----------------|
| `update_software` | Downloads tar.gz → extracts over project dir |
| `update_config` | Replaces `config.json` / `state_machine.json` / `pixel_config.json` |
| `update_appimage` | Replaces BambuStudio AppImage binary |
| `update_recording` | Replaces a single `recordings/<name>.json` |

All updates: download → SHA256 verify (refuses if missing) → backup → replace → optional restart. Path traversal protection in tar extraction via `member_path.resolve()` check.

### Cloud API Contract

```
POST /api/v1/agent/ready       — {hostname, version, status, timestamp}
GET  /api/v1/commands?status=pending  — returns {commands: [{type, data}]}
POST /api/v1/agent/status      — {hostname, status, task_id, message, timestamp}
POST /api/v1/agent/heartbeat   — {hostname, status, current_task_id, print_progress, uptime_seconds, timestamp}
```

Cloud message types: `print_task_a`, `print_task_b`, `cancel`, `restart`, `ping`, `update_software`, `update_config`, `update_appimage`, `update_recording`.

## Key Constraints

- **Must run under Xorg**, not Wayland. `xdotool` and PIL screenshots both require X11.
- Recording playback uses two backends: AutoHotkey (`.ahk` files, Windows) and Python `pynput` + `xdotool` (cross-platform). Recordings are stored as JSON with all mouse/keyboard events.
- `config.json` is the single source of truth for all module settings. `config.py` defines the schema via dataclasses. `cloud_config.json` is legacy backward-compat.
- The `PrinterController` enforces single-task concurrency: if `print_mgr.is_busy`, new tasks are rejected with `TASK_BUSY`.
- Print timeout can extend up to 3 extra rounds of `extra_timeout_minutes` before giving up.
