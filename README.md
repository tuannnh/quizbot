# QuizBot

Press **Ctrl + `** on any machine → the window under your mouse pointer is captured →
Claude reads the question and answers it → the answer appears on a live dashboard
(session, machine, question, answer) and as a desktop notification.

```
 Windows / Mac agent ──┐                       ┌──> Dashboard  http://<server>:8765
 (hotkey + capture)    ├─ POST /api/capture ──>│    (live via SSE, filter by session/machine)
 iPhone Shortcut ──────┘   (screenshot)        └──> Claude API (vision, structured answer)
                           QuizBot server (FastAPI + SQLite, Docker on home server)
```

Handles multiple choice, true/false, Kahoot (answer returned as shape + colour), open
questions and "other" (ordering, fill-in, maths). The model is forced to reply through one
tool schema, so every answer is structured: question, options, correct key(s), answer text,
confidence, short reasoning.

## 1. Server (home server)

```bash
cd server
cp .env.example .env        # set ANTHROPIC_API_KEY and QUIZBOT_TOKEN
docker compose up -d --build
```
Open `http://<home-server-ip>:8765`. Data (SQLite + captures) lives in `server/data/`.

Without Docker: `pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8765`.

Test without spending tokens: set `QUIZBOT_SOLVER=fake`.

Model choice (`QUIZBOT_MODEL`): `claude-sonnet-5-5` default; `claude-haiku-5-5` for
speed on timed Kahoot rounds; `claude-opus-5-5` for hard reasoning questions.

### Prebuilt image (GitHub Actions)

`.github/workflows/docker.yml` runs on every push to `main` and on `v*` tags:
1. runs `pytest` against the fake solver
2. builds the image, starts it, and checks `/healthz`
3. pushes a multi-arch image (`linux/amd64`, `linux/arm64`) to `ghcr.io/<owner>/quizbot`

Pull requests build and test the image but don't push it.

| Tag | When |
|---|---|
| `latest` | every push to `main` |
| `sha-<short>` | every build |
| `1.2.3`, `1.2` | when you push the tag `v1.2.3` |

On the home server, set `QUIZBOT_IMAGE=ghcr.io/<owner>/quizbot:latest` in `.env`, then run:
```bash
docker compose pull && docker compose up -d
```
If the repo is private, the package is private too. Either run
`docker login ghcr.io` with a personal access token that has `read:packages`, or make the
package public under GitHub > Packages > quizbot > Package settings.

Run the tests locally: `cd server && pip install -r requirements-dev.txt && pytest -q`.

## 2. Desktop agent

Needs Python 3.10+. In `agent/`:

| OS | Install (background, starts at login) | Uninstall |
|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File .\install_windows.ps1` | `... -Uninstall` |
| macOS | `./install_mac.sh` | `./install_mac.sh uninstall` |

First run creates `quizbot.env` — set `QUIZBOT_SERVER` (e.g. `http://192.168.1.10:8765`)
and `QUIZBOT_TOKEN`, then run the installer again. Optional: `QUIZBOT_SESSION` (default
today's date) and `QUIZBOT_MACHINE` (default hostname).

**macOS permissions** (one-time): grant the Python binary printed by the installer
*Accessibility* (hotkey) and *Screen Recording* (capture).

Quick test without the hotkey: `.venv/bin/python agent.py --once` (captures 3 s later).
Logs: `agent/agent.log`.

### Changing session, model and other settings

Use either of these:

1. **The dashboard (all machines at once).** Open **Settings** and set the *Active session* and
   *Model*. Every agent and the iPhone Shortcut use them, as long as their own
   `QUIZBOT_SESSION` / `QUIZBOT_MODEL` are empty. Leave the session blank to fall back to today's date.
   Saving needs the access token (`QUIZBOT_TOKEN`), which the browser remembers.
2. **`quizbot.env` (one machine).** Values set here override the dashboard for that machine.
   The agent re-reads the file on every capture, so edits apply on the next hotkey press with no restart.
   This also covers `QUIZBOT_SERVER`, `QUIZBOT_TOKEN`, `QUIZBOT_MACHINE` and `QUIZBOT_NOTIFY`.

Precedence for each capture: agent's `quizbot.env` → dashboard setting → default.

Restart the agent only when `agent.py` itself changes (after a `git pull`):
- Windows: re-run `install_windows.ps1` (it restarts the background process)
- macOS: `launchctl kickstart -k gui/$(id -u)/me.quizbot.agent`

### Clearing history

**Settings → Clear history** deletes answers and their stored screenshots. If a session is selected
in the filter, only that session is deleted; otherwise everything is. Needs the access token.
API: `DELETE /api/answers?session=<name>` (omit `session` for all).

### How capture works
- Windows: `WindowFromPoint` → top-level window → DWM frame bounds (no shadow), DPI-aware.
- macOS: topmost normal window (layer 0) containing the cursor via Quartz, captured with `screencapture`.
- Falls back to the whole monitor under the pointer if no window can be resolved.
- Server downscales to 1568 px longest edge before sending to Claude.

## Measuring speed

Every answer records where its time went. All values are in milliseconds:

| Stage | Measured by | What it is |
|---|---|---|
| `capture_ms` | agent | find the window under the pointer and take the screenshot |
| `network_ms` | agent − server | upload plus response over your LAN (round trip minus server time) |
| `prepare_ms` | server | resize to `QUIZBOT_MAX_EDGE` and encode as JPEG |
| `model_ms` | server | the Claude API call |
| `server_ms` | server | everything on the server from receiving the request to replying |
| `end_to_end_ms` | agent | hotkey press until the answer is back on the agent |

The dashboard shows a stacked bar for each answer and a **Speed by model** table (p50 / p90).
The agent log prints the same breakdown on every capture.

**Compare models live:** set `QUIZBOT_MODEL=claude-haiku-5-5` in one machine's `quizbot.env`
and leave the other machine on the server default. The table fills in as you use both.
Allowed models are set by `QUIZBOT_ALLOWED_MODELS` on the server.

**Compare offline on the same screenshots:** this replays saved captures, so the questions are identical across runs:
```bash
docker compose exec quizbot python -m app.bench \
  --models claude-haiku-5-5,claude-sonnet-5-5 --edges 1568,1024,768 --n 20 --repeat 2
```
It prints p50/p90/mean latency, input tokens, and how often each variant agreed with the
first model at the largest size. Agreement is a consistency check, not a measure of correctness.
Use `--images <folder>` to run on your own screenshots instead.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/capture` | multipart: `image`, `machine`, `session`, `source`, optional `model`, `capture_ms`; token via `X-QuizBot-Token` header or `token` form field. Returns the record plus a one-line `display`. |
| GET | `/api/answers?session=&machine=&limit=` | history, newest first |
| GET | `/api/answers/{id}/image` | stored capture |
| POST | `/api/answers/{id}/client-timing` | agent reports `roundtrip_ms` / `capture_ms` |
| GET | `/api/stats?session=` | p50/p90 per model for each stage |
| GET | `/api/models` | default and allowed models |
| GET / PUT | `/api/settings` | active session and model used when an agent sends none (PUT needs token) |
| DELETE | `/api/answers?session=` | clear one session, or all history (needs token) |
| GET | `/api/facets` | sessions and machines with counts |
| GET | `/api/events` | Server-Sent Events stream (pending + done records) |

## Known limits / next steps
- The hotkey isn't swallowed: Ctrl + ` still reaches the focused app (in VS Code it toggles the
  terminal; most other apps ignore it). If that clashes, set `QUIZBOT_HOTKEY` in `quizbot.env`
  (e.g. ``alt+` ``, ``ctrl+shift+` ``; on Mac also ``cmd+` ``) and restart the agent.
- Dashboard is open on the LAN (only capture needs the token). Put it behind your reverse proxy
  or add a token check if the network isn't trusted.
- iPhone Shortcut: next step. It will be "Take Screenshot → Get Contents of URL (POST form to
  `/api/capture`) → Show Notification(display)" — the server already accepts the token as a form field for this.
