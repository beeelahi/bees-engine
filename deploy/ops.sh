#!/bin/bash
# Operations commands for the private bees-ops repository (GitHub Actions -> SSH as "opsbot").
# opsbot's key can only run /usr/local/bin/bees-ops, which calls this script as user bees with ONE word from
# the list below (plus an optional number). Nothing else can be executed through that key.
set -euo pipefail
ROOT=/home/bees/BinanceData
CMD="${1:-status}"
N="${2:-60}"
[[ "$N" =~ ^[0-9]{1,4}$ ]] || N=60
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
scrub() { sed -E 's/(token|secret|key|signature|apikey)=[^& ]+/\1=***/Ig'; }   # belt and braces for log output

case "$CMD" in
  status)
    echo "== $(ts) $(hostname) =="
    echo "deployed: $(cut -c1-7 "$ROOT/.deployed_sha" 2>/dev/null || echo unknown)"
    systemctl list-timers bees-sync.timer bees-deploy.timer --no-pager | head -4
    for s in bees-sync bees-mm bees-mm-paper bees-deploy; do printf '%-12s %s\n' "$s" "$(systemctl is-active $s.service || true)"; done
    echo "-- last runs"; { grep -E "Run OK|Run FAILED|publish_site|Web export|live engine|ERROR" "$ROOT/logs/sync.log" || true; } | tail -n 12 | scrub
    echo "-- site"; cat "$ROOT/site_status.json" 2>/dev/null || echo "not published from this server yet"; echo
    echo "-- disk"; df -h / | tail -1; du -sh "$ROOT/data" 2>/dev/null || true
    echo "-- keys present (names only)"; for f in .github_token .telegram .binance_testnet .binance_live .mexc_live; do if [ -e "$ROOT/$f" ]; then printf '%s ' "$f"; fi; done; echo
    echo "-- settings"; python3 -c 'import json,sys; m=json.load(open(sys.argv[1])) if __import__("os").path.exists(sys.argv[1]) else {}; print("live.json mode:", m.get("mode","none"))' "$ROOT/live.json" 2>/dev/null || true
    python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("mm.json enabled:", m.get("enabled"))' "$ROOT/mm.json" 2>/dev/null || echo "mm.json: none"
    echo "-- database upkeep"; python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m.get("utc"), "problems:", m.get("problems") or "none"); [print(" ", k, v.get("check"), "|", v.get("backup", v.get("error", ""))) for k, v in m.get("databases", {}).items()]' "$ROOT/db_maint.json" 2>/dev/null || echo "not run yet"
    echo "-- switches"; for f in .no_publish STOP_TRADING FLATTEN_NOW MM_STOP; do if [ -e "$ROOT/$f" ]; then echo "$f is ON"; fi; done; echo "(none listed = all off)"
    ;;
  logs|sync-log)   tail -n "$N" "$ROOT/logs/sync.log" | scrub ;;
  deploy-log)      tail -n "$N" "$ROOT/logs/deploy.log" 2>/dev/null | scrub ;;
  mm-log)          sudo -n journalctl -u bees-mm --no-pager -n "$N" 2>/dev/null | scrub ;;
  health)
    last=$(grep -E "Run OK|Up to date" "$ROOT/logs/sync.log" | tail -1 | cut -c1-19)
    age=$(( $(date -u +%s) - $(date -u -d "$last" +%s 2>/dev/null || echo 0) ))
    echo "last good run: $last UTC ($((age / 60)) min ago)"
    [ "$age" -lt 3600 ] || { echo "UNHEALTHY: no good run for over an hour"; exit 1; }
    systemctl is-active --quiet bees-sync.timer || { echo "UNHEALTHY: sync timer not active"; exit 1; }
    echo healthy ;;
  run-sync)        sudo systemctl start --no-block bees-sync.service && echo "engine run started" ;;
  deploy)          sudo systemctl start bees-deploy.service && tail -n 5 "$ROOT/logs/deploy.log" ;;
  rollback)        "$ROOT/deploy/pull_deploy.sh" --rollback --now ;;
  mm-start)        rm -f "$ROOT/MM_STOP"; sudo systemctl restart bees-mm.service; echo "market maker (re)started; it trades only if mm.json enabled" ;;
  mm-paper-start)  rm -f "$ROOT/MM_PAPER_STOP"; sudo systemctl enable bees-mm-paper.service >/dev/null 2>&1 || true; sudo systemctl restart bees-mm-paper.service; sleep 20; systemctl is-active bees-mm-paper.service || true; sudo -n journalctl -u bees-mm-paper --no-pager -n 20 | scrub ;;
  mm-paper-stop)   sudo systemctl disable --now bees-mm-paper.service >/dev/null 2>&1 || true; echo "paper market maker stopped (results kept in data/mm_paper_*.db)" ;;
  mm-paper-report) python3 "$ROOT/bin/mm_paper.py" --report ;;
  mm-paper-data)   python3 "$ROOT/bin/mm_paper.py" --json ;;
  mm-paper-log)    sudo -n journalctl -u bees-mm-paper --no-pager -n "$N" 2>/dev/null | scrub ;;
  mm-stop)         touch "$ROOT/MM_STOP"; echo "MM_STOP set: the bot cancels its own orders and pauses (it never market-sells)" ;;
  trading-stop)    touch "$ROOT/STOP_TRADING"; echo "STOP_TRADING set: the live engine places no new entries" ;;
  trading-resume)  rm -f "$ROOT/STOP_TRADING"; echo "STOP_TRADING cleared" ;;
  carry-study)     nice -n 10 python3 "$ROOT/bin/carry_study.py" ;;
  shortlist-study) nice -n 10 python3 "$ROOT/bin/shortlist_study.py" ;;
  calls-study)     setsid nohup nice -n 15 python3 "$ROOT/bin/calls_study.py" > "$ROOT/logs/calls_study.txt" 2>&1 < /dev/null &
                   echo "channel calls study started in the background; read it with study-log" ;;
  study-log)       tail -n "$N" "$ROOT/logs/calls_study.txt" 2>/dev/null || echo "no study output yet" ;;
  bee-study)       setsid nohup nice -n 15 python3 -u "$ROOT/research/mm_bee_multi.py" > "$ROOT/logs/bee_study.txt" 2>&1 < /dev/null &
                   echo "MM-Bee multi-coin study started in the background; read it with bee-study-log" ;;
  bee-study-log)   tail -n "$N" "$ROOT/logs/bee_study.txt" 2>/dev/null || echo "no MM-Bee study output yet" ;;
  *) echo "unknown command: $CMD"; echo "commands: status logs deploy-log mm-log health run-sync deploy rollback mm-start mm-stop mm-paper-start mm-paper-stop mm-paper-report mm-paper-data mm-paper-log trading-stop trading-resume carry-study shortlist-study calls-study study-log bee-study bee-study-log"; exit 2 ;;
esac
