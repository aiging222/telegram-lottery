#!/usr/bin/env bash
# Update the bot on the server in one go. Run as root:
#   bash /home/lottery/telegram-lottery/scripts/update.sh
# Stops the bot, backs up the database (keeping the last 5 backups), pulls main, installs
# the locked dependencies and starts the bot again, also when one of the steps fails.
set -euo pipefail

main() {
    local dir
    dir="$(cd "$(dirname "$0")/.." && pwd)"
    systemctl stop lottery-bot
    trap 'systemctl start lottery-bot; systemctl status lottery-bot --no-pager -n 5' EXIT
    sudo -u lottery bash -lc "
        set -euo pipefail
        cd '$dir'
        cp data/lottery.sqlite3 \"data/backup-\$(date +%Y%m%d-%H%M%S).sqlite3\"
        ls -t data/backup-*.sqlite3 | tail -n +6 | xargs -r rm --
        git pull --ff-only
        uv sync --locked --no-dev
        git log --oneline -1
    "
}

# Bash reads a script as it runs; calling main from here means the whole file has been
# read before git pull can change it.
main "$@"
exit
