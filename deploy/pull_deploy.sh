#!/bin/bash
# Continuous deployment, pull-based: the server fetches main from the public repository, and deploys a commit
# only when every GitHub check on it has passed AND the test suite passes again on this server.
# GitHub never holds a credential for this server; nothing is pushed to it.
#
#   pull_deploy.sh            normal run (bees-deploy.timer, every 5 minutes)
#   pull_deploy.sh --now      same, but log to the terminal
#   pull_deploy.sh --rollback switch back to the previous release
set -euo pipefail
umask 077

REPO="${BEES_REPO:-beeelahi/bees-engine}"
BRANCH="${BEES_BRANCH:-main}"
APP="${BEES_APP:-/home/bees/app}"                # git checkout of the repository (read-only use)
ROOT="${BEES_ROOT:-/home/bees/BinanceData}"      # live install: code dirs + private data, settings and keys
RELEASES="${BEES_RELEASES:-/home/bees/releases}" # unpacked, tested releases (last 5 kept)
GIT_URL="${BEES_GIT_URL:-https://github.com/$REPO.git}"
PARTS="bin web deploy picks research"
mkdir -p "$ROOT/logs" "$RELEASES"
[ "${1:-}" = "--now" ] || exec >>"$ROOT/logs/deploy.log" 2>&1
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

notify() {  # Telegram, if alerts are set up; never fails the deploy
  ROOT="$ROOT" python3 - "$1" <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "bin"))
import alerts
c = alerts.read_cred()
if c:
    alerts.send(*c, sys.argv[1])
PY
}

switch_to() {  # $1 = unpacked release dir, $2 = sha
  local rel="$1" sha="$2"
  exec 9>"$ROOT/data/.lock" 2>/dev/null || exec 9>/tmp/bees.lock
  flock -w 600 9 || { echo "$(ts) engine busy for 10 min, will retry"; exit 0; }   # never swap mid-run
  for p in $PARTS; do
    rm -rf "$ROOT/$p.new"
    [ -e "$rel/$p" ] && cp -a "$rel/$p" "$ROOT/$p.new"
  done
  for p in $PARTS; do
    [ -e "$ROOT/$p.new" ] || continue
    rm -rf "$ROOT/$p.prev"
    [ -e "$ROOT/$p" ] && mv "$ROOT/$p" "$ROOT/$p.prev"
    mv "$ROOT/$p.new" "$ROOT/$p"
  done
  echo "$sha" > "$ROOT/.deployed_sha"
  echo "$sha" >> "$ROOT/.deploy_history"
  flock -u 9
  command -v systemctl >/dev/null || return 0
  # system units: install when they changed (bees has passwordless sudo for this)
  local changed=0
  for u in "$ROOT"/deploy/systemd/*; do
    cmp -s "$u" "/etc/systemd/system/$(basename "$u")" && continue
    if sudo -n install -m 644 "$u" /etc/systemd/system/; then changed=1; else echo "$(ts) could not install $(basename "$u")"; fi
  done
  if [ "$changed" = 1 ]; then
    sudo -n systemctl daemon-reload || true
    sudo -n systemctl enable --now bees-sync.timer bees-deploy.timer >/dev/null 2>&1 || true
  fi
  if systemctl is-active --quiet bees-mm.service; then sudo -n systemctl restart bees-mm.service || true; fi
  if systemctl is-active --quiet bees-mm-paper.service; then sudo -n systemctl restart bees-mm-paper.service || true; fi
}

if [ "${1:-}" = "--rollback" ]; then
  cur=$(cat "$ROOT/.deployed_sha" 2>/dev/null || true)
  prev=""
  while read -r h; do
    [[ "$h" =~ ^[0-9a-f]{40}$ ]] && [ "$h" != "$cur" ] && [ -d "$RELEASES/$h" ] && prev="$h"
  done < <(grep -v "^${cur:-x}\$" "$ROOT/.deploy_history" 2>/dev/null || true)
  [ -n "$prev" ] || { echo "$(ts) no previous release to roll back to"; exit 1; }
  switch_to "$RELEASES/$prev" "$prev"
  echo "$(ts) rolled back to ${prev:0:7}"; notify "Engine rolled back to ${prev:0:7}"
  exit 0
fi

[ -d "$APP/.git" ] || git clone -q --branch "$BRANCH" "$GIT_URL" "$APP"
git -C "$APP" fetch -q --prune origin "$BRANCH"
NEW=$(git -C "$APP" rev-parse "origin/$BRANCH")
CUR=$(cat "$ROOT/.deployed_sha" 2>/dev/null || echo none)
[ "$NEW" = "$CUR" ] && exit 0
[ -f "$RELEASES/$NEW.failed" ] && exit 0            # this commit already failed its tests here

# Gate 1: every check run on the commit (CI, CodeQL, secret scan) finished green
STATE=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/commits/$NEW/check-runs?per_page=100" 2>/dev/null | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("check_runs", [])
except ValueError:
    print("unknown"); sys.exit()
if not runs: print("pending"); sys.exit()
if any(r["status"] != "completed" and not r.get("conclusion") for r in runs): print("pending"); sys.exit()   # a run GitHub left "in progress" with a result counts as done
bad = [r["name"] for r in runs if r["conclusion"] not in ("success", "neutral", "skipped")]
print("failed:" + ",".join(bad) if bad else "success")') || STATE="unknown"
[ "${BEES_TEST_SKIP_CHECKS:-}" = "yes-this-is-a-test" ] && STATE=success      # test harness only
if [ "$STATE" != "success" ]; then
  case "$STATE" in failed*) echo "$(ts) ${NEW:0:7}: checks $STATE, not deploying";; esac
  exit 0
fi

# Gate 2: unpack and run the test suite here, on this server's Python, away from live data
REL="$RELEASES/$NEW"
rm -rf "$REL" && mkdir -p "$REL"
git -C "$APP" archive "$NEW" | tar -x -C "$REL"
if ! (cd "$REL" && python3 -m unittest discover -s tests -q >"$REL.test.log" 2>&1); then
  touch "$RELEASES/$NEW.failed"
  echo "$(ts) ${NEW:0:7}: tests FAILED on the server, not deploying (see $REL.test.log)"
  notify "Deploy blocked: tests failed on the server for ${NEW:0:7}"
  exit 1
fi

SUBJECT=$(git -C "$APP" log -1 --format=%s "$NEW")
switch_to "$REL" "$NEW"
echo "$(ts) deployed ${NEW:0:7} ($SUBJECT), previous ${CUR:0:7}"
notify "Engine updated to ${NEW:0:7}: $SUBJECT"

# keep the 5 newest releases
keep=$(tail -n 5 "$ROOT/.deploy_history" | sort -u)
for dir in "$RELEASES"/*/; do
  h=$(basename "$dir")
  [[ "$h" =~ ^[0-9a-f]{40}$ ]] || continue
  grep -qx "$h" <<< "$keep" || rm -rf "${RELEASES:?}/$h" "$RELEASES/$h.test.log"
done
