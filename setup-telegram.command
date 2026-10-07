#!/bin/zsh
set -euo pipefail
task_root="${0:A:h}"
cd "$task_root"
"$task_root/.venv-analysis/bin/python" -m telegram_analysis.setup --all-chats
