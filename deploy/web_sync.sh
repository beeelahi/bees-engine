#!/bin/bash
# Copy the built site to the web server's folder. Skips while a publish is rebuilding the site (index.html or the
# data package not there yet), so visitors never see a half-built site. Never copies .git.
set -euo pipefail
S=/home/bees/BinanceData/site_build
W=/var/www/beesells
[ -f "$S/index.html" ] && [ -f "$S/data/overview.json" ] || exit 0
[ -d "$W" ] || exit 0
rsync -a --delete --delay-updates --exclude '.git' --exclude '.git/' "$S/" "$W/"
