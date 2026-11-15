#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'from PIL import Image; assert 11 <= int(Image.__version__.split(".")[0]) < 14' >/dev/null 2>&1; then
  if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m ensurepip
  fi
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python kindle_dashboard.py serve "$@"
