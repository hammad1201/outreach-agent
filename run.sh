#!/bin/zsh
# Runs one business's outreach and keeps the Mac from sleeping while it does (on power; a closed
# lid on battery still sleeps). Used by the background service; you can also run it directly:
#   ./run.sh ~/path/to/home
CODE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${1:-$OUTREACH_HOME}"
[ -n "$HOME_DIR" ] || { echo "usage: run.sh <home folder>"; exit 1; }
cd "$HOME_DIR" || exit 1
export PYTHONPATH="$CODE${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/caffeinate -is "$CODE/.venv/bin/python" -m outreach_agent --home "$HOME_DIR" run
