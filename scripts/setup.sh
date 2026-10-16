#!/bin/zsh
# Walks through setup for one home folder, checks everything, then installs the background service
# so outreach runs by itself from now on, including after restarts. Called by <home>/Setup.command.
CODE="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$1"
[ -d "$HOME_DIR" ] || { echo "usage: setup.sh <home folder>"; exit 1; }
cd "$HOME_DIR" || exit 1
export PYTHONPATH="$CODE${PYTHONPATH:+:$PYTHONPATH}"
PY="$CODE/.venv/bin/python"
OA=("$PY" -m outreach_agent --home "$HOME_DIR")

pause() { echo; read -k1 "?$1"; echo; }

clear
echo "🧭  Outreach setup — $HOME_DIR"
echo "────────────────────────────"

# 0. Python environment
if [ ! -x "$PY" ]; then
  echo "\nInstalling the Python packages (first run only)…"
  python3 -m venv "$CODE/.venv" && "$CODE/.venv/bin/pip" install -q -r "$CODE/requirements.txt" || {
    echo "❌ Couldn't set up Python. Install Python 3.11+ (e.g. brew install python) and try again."
    pause "Press any key to close."; exit 1; }
fi

# 1. Claude (only when using the subscription)
if grep -q '^provider *= *"cli"' config.toml; then
  CLAUDE="$("${OA[@]}" claude-path 2>/dev/null)"
  if [ -z "$CLAUDE" ]; then
    echo "\n❌ Claude Code isn't installed. Install it (https://claude.com/claude-code), then run Setup again."
    pause "Press any key to close."; exit 1
  fi
  if ! "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    echo "\nStep 1 — log in to Claude."
    echo "Claude will start here. Follow its prompts to log in (a browser window opens)."
    echo "When you see Claude's prompt, type /exit and press Return to come back to setup."
    pause "Press any key to start…"
    "$CLAUDE"
  fi
  if "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    echo "✅ Claude is logged in."
  else
    echo "❌ Claude still isn't logged in. Double-click Setup again to retry."
    pause "Press any key to close."; exit 1
  fi
fi

# 2. Secrets
[ -f .env ] || { cp "$CODE/.env.example" .env; chmod 600 .env; }
if grep -qE '^(DISCORD_BOT_TOKEN|DISCORD_CHANNEL_ID|DISCORD_OWNER_ID|SMTP_USER|SMTP_PASSWORD)=$' .env; then
  echo "\nStep 2 — fill in your keys."
  echo "TextEdit will open .env. Paste the Discord token and IDs, your Gmail address and app password."
  echo "Save (⌘S) and close it."
  open -e .env
  pause "Press any key once you've saved .env…"
fi

# 3. Postal address
if grep -q '^postal_address *= *""' config.toml; then
  echo "\nStep 3 — postal address (optional)."
  echo "US and Canadian law require a postal address in every email. Without one, outreach still runs"
  echo "but skips US and Canadian companies. You can add one any time later."
  read -k1 "?Add an address now? (y/N) "; echo
  if [[ "$REPLY" == [yY] ]]; then
    echo "TextEdit will open config.toml. Put the address between the quotes on the postal_address line,"
    echo "then save (⌘S) and close."
    open -e config.toml
    pause "Press any key once you've saved config.toml…"
  fi
fi

# 4. Check everything
echo "\nStep 4 — checking every connection (this makes one real Claude call)…\n"
if ! "${OA[@]}" doctor --live; then
  echo "\nFix the ❌ items above, then double-click Setup again."
  pause "Press any key to close."; exit 1
fi

# 5. Run it in the background from now on
echo "\nStep 5 — starting it in the background…"
if "${OA[@]}" install-service; then
  echo "\n✅ Done. Outreach now runs by itself, and starts again whenever you log in."
  echo "Everything from here happens in Discord. To stop it, double-click Stop."
else
  echo "\n❌ Couldn't install the background service — see the message above."
fi
pause "Press any key to close."
