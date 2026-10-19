#!/usr/bin/env bash
# QuizBot agent installer for macOS (runs as a LaunchAgent at login).
#   ./install_mac.sh            install / update and start
#   ./install_mac.sh uninstall  stop and remove
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="me.quizbot.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV="$HERE/.venv"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "QuizBot agent removed."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required (brew install python)"; exit 1; }
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r "$HERE/requirements.txt"

if [[ ! -f "$HERE/quizbot.env" ]]; then
  cp "$HERE/quizbot.env.example" "$HERE/quizbot.env"
  echo "Created quizbot.env - edit QUIZBOT_SERVER and QUIZBOT_TOKEN, then re-run ./install_mac.sh"
  open -e "$HERE/quizbot.env"
  exit 0
fi

# Resolve the real interpreter: macOS privacy permissions attach to this binary.
PY_REAL="$("$VENV/bin/python" -c 'import os,sys; print(os.path.realpath(sys.executable))')"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$VENV/bin/python</string><string>$HERE/agent.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$HERE/agent.stderr.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

cat <<EOF

QuizBot agent installed and running (starts at login).

One-time macOS permissions - grant BOTH to this binary, then run: ./install_mac.sh
  $PY_REAL
  1. System Settings > Privacy & Security > Accessibility      (hotkey)
  2. System Settings > Privacy & Security > Screen Recording   (capture)
  Use the "+" button and press Cmd+Shift+G to paste the path above.

Log: $HERE/agent.log
EOF
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" || true
