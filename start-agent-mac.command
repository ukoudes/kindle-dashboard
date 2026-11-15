#!/bin/zsh
set -e
cd "$(dirname "$0")"
exec python3 kindle_dashboard.py agent --config clients/agent.json
