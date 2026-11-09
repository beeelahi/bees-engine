#!/bin/bash
# Copy the built site to the web server's folder. Skips while a publish is rebuilding the site (index.html or the
# data package not there yet), so visitors never see a half-built site. Never copies .git.
set -euo pipefail
S=/home/bees/BinanceData/site_build
W=/var/www/beesells
[ -f "$S/index.html" ] && [ -f "$S/data/overview.json" ] || exit 0
[ -d "$W" ] || exit 0
# public copy: world-readable so the web server (its own user) can read it; it holds only the public site
rsync -rltp --delete --delay-updates --chmod=D755,F644 --exclude '.git' --exclude '.git/' "$S/" "$W/"
