#!/usr/bin/env bash
# Update the bot on the server in one go. Run as root:
#   bash /home/lottery/telegram-lottery/scripts/update.sh
# Stops the bot, backs up the database (keeping the last 5 backups next to it), pulls main,
# installs the locked dependencies the way the project was installed (pip or uv) and starts
# the bot again, also when one of the steps fails.
set -euo pipefail

main() {
    local dir steps
    dir="$(cd "$(dirname "$0")/.." && pwd)"
    # Run as the bot's user in a login shell, so that uv is on the PATH; $1 is the project.
    IFS= read -r -d '' steps <<'STEPS' || true
set -euo pipefail
cd "$1"
# The database the bot uses: DATABASE_PATH from .env, relative to the project directory.
db="$(.venv/bin/python -c 'from dotenv import dotenv_values
print(dotenv_values(".env").get("DATABASE_PATH") or "data/lottery.sqlite3")')"
if [ -f "$db" ]; then
    # SQLite's own backup, which also takes what is still in the -wal file beside it.
    .venv/bin/python -c 'import sqlite3, sys
source, backup = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
source.backup(backup)
backup.close()
source.close()' \
        "$db" "$(dirname "$db")/backup-$(date +%Y%m%d-%H%M%S).sqlite3"
    ls -t "$(dirname "$db")"/backup-*.sqlite3 | tail -n +6 | while IFS= read -r old; do
        rm -- "$old"
    done
fi
git pull --ff-only
# An environment set up with pip has pip in it; one made by uv has none.
if [ -x .venv/bin/pip ]; then
    .venv/bin/python -m pip install -r requirements.lock
    .venv/bin/python -m pip install --no-deps -e .
else
    uv sync --locked --no-dev
fi
git log --oneline -1
STEPS
    systemctl stop lottery-bot
    trap 'systemctl start lottery-bot; systemctl status lottery-bot --no-pager -n 5' EXIT
    sudo -u lottery bash -lc "$steps" update "$dir"
}

# Bash reads a script as it runs; calling main from here means the whole file has been
# read before git pull can change it.
main "$@"
exit
