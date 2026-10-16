#!/bin/zsh
# Stops one home folder's outreach completely, including at future logins. Called by <home>/Stop.command.
CODE="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$CODE${PYTHONPATH:+:$PYTHONPATH}"
"$CODE/.venv/bin/python" -m outreach_agent --home "$1" uninstall-service
echo; read -k1 "?Press any key to close."
