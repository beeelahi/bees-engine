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
  mm-preflight)    python3 "$ROOT/bin/mm_preflight.py" 2>&1 | scrub ;;
  dns-check)       python3 "$ROOT/bin/dns_check.py" 2>&1 | scrub ;;
  web-setup)       bash "$ROOT/deploy/web_setup.sh" 2>&1 | scrub ;;
  web-status)
    echo "caddy: $(systemctl is-active caddy 2>/dev/null) | copy timer: $(systemctl is-active bees-web-sync.timer 2>/dev/null) | publish: $([ -f "$ROOT/.publish_local_only" ] && echo 'this server only (GitHub Pages not updated)' || echo 'GitHub Pages')"
    echo "local site: $(curl -s -o /dev/null -w 'HTTP %{http_code}' http://127.0.0.1:8088/ 2>/dev/null); data built $(python3 -c 'import json; print(json.load(open("/var/www/beesells/data/overview.json")).get("generated_utc"))' 2>/dev/null || echo '?')"
    echo "public https: $(curl -s -o /dev/null -w 'HTTP %{http_code}' --max-time 15 --resolve beesells.xyz:443:127.0.0.1 https://beesells.xyz/ 2>/dev/null || echo 'no certificate yet')"
    sudo -n journalctl -u caddy --no-pager -n 400 2>/dev/null | grep -iE "certificate obtained|obtain|error" | tail -n 6 | cut -c1-260 | scrub
    ;;
  publish-local)   touch "$ROOT/.publish_local_only" && echo "publish: this server only (GitHub Pages will no longer be updated)" ;;
  publish-github)  rm -f "$ROOT/.publish_local_only" && echo "publish: GitHub Pages again" ;;
  world-probe)     python3 "$ROOT/bin/world_markets.py" --probe 2>&1 | scrub ;;
  world-refresh)   python3 "$ROOT/bin/world_markets.py" 2>&1 | tail -n "$N" | scrub ;;
  live-drill)      python3 "$ROOT/bin/live_engine.py" --testnet-drill 2>&1 | scrub ;;   # Binance TESTNET only (fake money)
  mm-plan)         python3 "$ROOT/bin/mm_plan.py" 2>&1 | scrub ;;
  mm-status)
    echo "bees-mm: $(systemctl is-active bees-mm.service 2>/dev/null) | kill switch MM_STOP: $([ -f "$ROOT/MM_STOP" ] && echo SET || echo off)"
    hb="$ROOT/data/mm_heartbeat.json"
    if [ -f "$hb" ]; then echo "heartbeat $(( $(date +%s) - $(stat -c %Y "$hb") )) s ago"; cat "$hb"; else echo "no heartbeat yet (the market maker has not run a loop)"; fi
    sudo -n journalctl -u bees-mm --no-pager -n 15 2>/dev/null | scrub ;;
  health)
    last=$(grep -E "Run OK|Up to date" "$ROOT/logs/sync.log" | tail -1 | cut -c1-19)
    age=$(( $(date -u +%s) - $(date -u -d "$last" +%s 2>/dev/null || echo 0) ))
    echo "last good run: $last UTC ($((age / 60)) min ago)"
    [ "$age" -lt 3600 ] || { echo "UNHEALTHY: no good run for over an hour"; exit 1; }
    systemctl is-active --quiet bees-sync.timer || { echo "UNHEALTHY: sync timer not active"; exit 1; }
    if systemctl is-active --quiet bees-mm.service && [ ! -f "$ROOT/MM_STOP" ] && grep -q '"enabled": *true' "$ROOT/mm.json" 2>/dev/null; then
      hb="$ROOT/data/mm_heartbeat.json"; hage=$(( $(date +%s) - $(stat -c %Y "$hb" 2>/dev/null || echo 0) ))
      echo "market maker heartbeat: $hage s ago"
      [ "$hage" -lt 300 ] || { echo "UNHEALTHY: market maker running but no heartbeat for over 5 minutes"; exit 1; }
    fi
    # every published data file must be fresh: most refresh at least daily, the moving-average study every 4 hours
    stale=""; now=$(date +%s)
    for f in "$ROOT"/web_export/*.json; do
      [ -f "$f" ] || continue; n=$(basename "$f"); lim=93600
      case "$n" in ma_lab.json|ma_summary.json) lim=21600 ;; esac
      a=$(( now - $(stat -c %Y "$f") )); [ "$a" -gt "$lim" ] && stale="$stale $n($((a / 3600))h)"
    done
    [ -z "$stale" ] || { echo "UNHEALTHY: stale data files:$stale"; exit 1; }
    python3 "$ROOT/bin/publish_site.py" --health || exit 1   # fresh files are not enough: did they reach the website?
    systemctl is-active --quiet bees-ma-lab.timer || echo "note: moving-average timer not active yet (it is enabled on the next deploy)"
    echo healthy ;;
  saad-user)       # Saad's own login with admin rights (sudo without a password: the account has no password at all,
                   # login is by his key only). Bilal's request, 15 Nov 2026. Idempotent. Remove: sudo deluser --remove-home saad
    K="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMR76KAyM4/8zwmV+Vhmi5Zr34KABC7D/7g7Aj/ue6nC saad"
    id saad >/dev/null 2>&1 || sudo -n adduser --disabled-password --gecos "Saad" saad >/dev/null
    sudo -n usermod -aG sudo saad
    sudo -n install -d -o saad -g saad -m 700 /home/saad/.ssh
    echo "$K" | sudo -n tee /home/saad/.ssh/authorized_keys >/dev/null
    sudo -n chown saad:saad /home/saad/.ssh/authorized_keys; sudo -n chmod 600 /home/saad/.ssh/authorized_keys
    echo "saad ALL=(ALL) NOPASSWD:ALL" | sudo -n tee /etc/sudoers.d/90-saad >/dev/null; sudo -n chmod 440 /etc/sudoers.d/90-saad
    sudo -n visudo -cf /etc/sudoers.d/90-saad >/dev/null && echo "sudo rule ok" || { sudo -n rm -f /etc/sudoers.d/90-saad; echo "sudo rule invalid: removed"; }
    # SSH only admits users on its AllowUsers list (bees, opsbot); AllowUsers lines add up, so this adds saad to it
    printf '%s\n' "# Bee's Dashboard ops (saad-user): Saad's own login" "AllowUsers saad" | sudo -n tee /etc/ssh/sshd_config.d/00-bees-saad.conf >/dev/null
    if sudo -n sshd -t; then sudo -n systemctl reload ssh; echo "ssh: saad allowed, reloaded"; else sudo -n rm -f /etc/ssh/sshd_config.d/00-bees-saad.conf; echo "ssh config test FAILED: removed"; fi
    echo "user: $(id saad)"; echo "key: $(sudo -n ssh-keygen -lf /home/saad/.ssh/authorized_keys)" ;&
  ssh-check)       # why can't someone reach SSH? what sshd listens on, the firewall, fail2ban, recent refusals
    echo "listening: $(sudo -n ss -ltnp 2>/dev/null | awk '/sshd/{print $4}' | tr '\n' ' ')"
    echo "sshd: $(sudo -n sshd -T 2>/dev/null | grep -Ei '^(port|listenaddress|permitrootlogin|passwordauthentication|allowusers|allowgroups|denyusers) ' | tr '\n' ' ')"
    echo "ssh service: $(systemctl is-active ssh 2>/dev/null) socket: $(systemctl is-active ssh.socket 2>/dev/null)"
    echo "ufw:"; sudo -n ufw status verbose 2>/dev/null | sed -n '1,25p'
    command -v fail2ban-client >/dev/null && { echo "fail2ban:"; sudo -n fail2ban-client status sshd 2>/dev/null | sed -n '1,12p'; }
    echo "recent ssh log:"; sudo -n journalctl -u ssh --since "-3h" --no-pager 2>/dev/null | grep -Ei "accepted|refused|invalid|failed|closed" | tail -12 | sed -E 's/([0-9]+\.[0-9]+)\.[0-9]+\.[0-9]+/\1.x.x/g' ;;
  site-token-save) # new website token arriving on stdin from the bees-ops "Save site token" workflow (GitHub secret
                   # SITE_TOKEN, entered by Bilal). Checked with GitHub first; the file is only replaced if GitHub accepts it.
    F="$ROOT/.github_token"; T=""
    IFS= read -r -t 20 T || true
    T="${T%$'\r'}"
    [[ "$T" =~ ^(github_pat_|ghp_)[A-Za-z0-9_]{20,250}$ ]] || { echo "REFUSED: that does not look like a GitHub token (nothing changed)"; exit 1; }
    R=$(printf '%s' "$T" | python3 -c '
import json, sys, urllib.request
t = sys.stdin.read()
try:
    r = urllib.request.urlopen(urllib.request.Request("https://api.github.com/repos/beeelahi/beesells",
        headers={"Authorization": "Bearer " + t, "User-Agent": "BeesDashboard"}), timeout=20)
    d = json.load(r); print("ok" if d.get("permissions", {}).get("push") else "nopush", r.headers.get("github-authentication-token-expiration") or "no-expiry")
except urllib.error.HTTPError as e: print("rejected", e.code)
except Exception as e: print("error", type(e).__name__)')
    case "$R" in
      ok*) umask 077; printf '%s\n' "$T" > "$F.new" && chmod 600 "$F.new" && mv -f "$F.new" "$F"; unset T
           echo "saved: GitHub accepts the new token, it can write to beeelahi/beesells, expires ${R#ok }" ;;
      nopush*) unset T; echo "REFUSED: GitHub accepts the token but it cannot write to beeelahi/beesells (needs Contents: Read and write). Nothing changed." ; exit 1 ;;
      *) unset T; echo "REFUSED: GitHub did not accept the token ($R). Nothing changed."; exit 1 ;;
    esac ;;
  token-check)     # the GitHub token's state WITHOUT showing it: file facts, format, and whether GitHub accepts it
    F="$ROOT/.github_token"
    echo "file: $(stat -c '%U:%G mode %a, %s bytes, changed %y' "$F" 2>&1)"
    [ -r "$F" ] && echo "readable by $(whoami): yes" || echo "readable by $(whoami): NO"
    python3 - "$F" <<'PY'
import re, sys, json, urllib.request
p = sys.argv[1]
try:
    raw = open(p).read()
except Exception as e:
    print("read error:", type(e).__name__); sys.exit()
t = raw.strip()
print("lines:", raw.count("\n"), "| length:", len(t), "| kind:", "fine-grained" if t.startswith("github_pat_") else "classic" if t.startswith("ghp_") else "unknown prefix",
      "| format ok:", bool(re.fullmatch(r"[A-Za-z0-9_]{20,255}", t)), "| spaces/CR inside:", bool(re.search(r"\s", t)))
try:
    r = urllib.request.urlopen(urllib.request.Request("https://api.github.com/repos/beeelahi/beesells",
        headers={"Authorization": "Bearer " + t, "User-Agent": "BeesDashboard"}), timeout=20)
    d = json.load(r)
    print("GitHub: accepted | push allowed:", d.get("permissions", {}).get("push"), "| expires:", r.headers.get("github-authentication-token-expiration"))
except urllib.error.HTTPError as e:
    print("GitHub: REJECTED, HTTP", e.code, (e.read()[:160]).decode("utf8", "replace"))
except Exception as e:
    print("GitHub check failed:", type(e).__name__)
PY
    echo "git: $(git --version) | global config: $(git config --global --list 2>/dev/null | grep -v -i 'token\|extraheader' | tr '\n' ' ')" ;;
  root-login-off)  # close root SSH login again (undo root-login-keys)
    sudo -n rm -f /etc/ssh/sshd_config.d/00-bees-root-keys.conf
    sudo -n sshd -t && { sudo -n systemctl reload ssh 2>/dev/null || sudo -n systemctl reload sshd; } && echo "root login closed again"
    echo "sshd: $(sudo -n sshd -T 2>/dev/null | grep -Ei '^(permitrootlogin|passwordauthentication) ' | tr '\n' ' ')" ;;
  run-sync)        sudo systemctl start --no-block bees-sync.service && echo "engine run started" ;;
  root-key-saad)   # Saad's public key on root's authorized keys (Bilal's request, 15 Nov 2026). Idempotent. Remove: delete the line.
    K="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMR76KAyM4/8zwmV+Vhmi5Zr34KABC7D/7g7Aj/ue6nC saad"
    sudo -n install -d -o root -g root -m 700 /root/.ssh
    sudo -n test -f /root/.ssh/authorized_keys || sudo -n install -o root -g root -m 600 /dev/null /root/.ssh/authorized_keys
    if sudo -n grep -qF "${K% *}" /root/.ssh/authorized_keys; then echo "already there"; else echo "$K" | sudo -n tee -a /root/.ssh/authorized_keys >/dev/null && echo "added"; fi
    sudo -n chmod 600 /root/.ssh/authorized_keys; sudo -n chown root:root /root/.ssh/authorized_keys ;&
  root-login-keys) # let root log in over SSH with a key only (passwords stay off); Bilal's request, 15 Nov 2026.
    # Our file sorts first in sshd_config.d, and sshd keeps the first value it reads, so it wins over later files.
    echo "drop-ins before: $(ls /etc/ssh/sshd_config.d/ 2>/dev/null | tr '\n' ' ')"
    printf '%s\n' "# Bee's Dashboard ops (root-login-keys): root may log in with a key, never a password" \
      "PermitRootLogin prohibit-password" "PasswordAuthentication no" | sudo -n tee /etc/ssh/sshd_config.d/00-bees-root-keys.conf >/dev/null
    if sudo -n sshd -t; then sudo -n systemctl reload ssh 2>/dev/null || sudo -n systemctl reload sshd; echo "reloaded (open sessions are not touched)"
    else sudo -n rm -f /etc/ssh/sshd_config.d/00-bees-root-keys.conf; echo "config test FAILED: change removed, nothing reloaded"; fi ;&
  root-keys)       # fingerprints only (no key material), and whether sshd lets root in with a key
    echo "root authorized keys:"; sudo -n ssh-keygen -lf /root/.ssh/authorized_keys 2>/dev/null || echo "  none"
    echo "sshd: $(sudo -n sshd -T 2>/dev/null | grep -Ei '^(permitrootlogin|passwordauthentication|pubkeyauthentication) ' | tr '\n' ' ')" ;;
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
  call-lab)        setsid nohup nice -n 15 python3 "$ROOT/research/calls_lab.py" > "$ROOT/logs/calls_lab.txt" 2>&1 < /dev/null &
                   echo "calls lab (divergences + call rules) started in the background; read it with call-lab-log" ;;
  onchain-probe)   python3 "$ROOT/bin/onchain_stocks.py" --probe 2>&1 | head -n 120 ;;
  onchain-refresh) python3 "$ROOT/bin/onchain_stocks.py" 2>&1 | tail -n 20 ;;
  strategy-lab)    python3 "$ROOT/research/strategy_lab.py" > "$ROOT/logs/strategy_lab.txt" 2>&1; tail -n 20 "$ROOT/logs/strategy_lab.txt" ;;
  strategy-lab-log) tail -n "$N" "$ROOT/logs/strategy_lab.txt" 2>/dev/null || echo "no strategy lab output yet" ;;
  ma-lab)          sudo -n systemctl start --no-block bees-ma-lab.service \
                     && echo "moving-average lab started (unit bees-ma-lab, also runs every 4 hours by timer); read it with ma-lab-log" \
                     || echo "could not start bees-ma-lab" ;;
  ma-lab-log)      tail -n "$N" "$ROOT/logs/ma_lab.txt" 2>/dev/null || echo "no moving-average lab output yet" ;;
  zig-probe)       python3 "$ROOT/bin/zig_archive.py" --probe 2>&1 | tail -n 20 ;;
  zig-status)      python3 "$ROOT/bin/zig_archive.py" --status 2>&1 | tail -n 10 ;;
  library-probe)   python3 "$ROOT/bin/coin_library.py" --probe 2>&1 | head -n 80; python3 "$ROOT/bin/coin_library.py" --tags 2>&1 | head -n 120 ;;
  library-refresh) python3 "$ROOT/bin/coin_library.py" 2>&1 | tail -n "$N" ;;
  library-status)  python3 "$ROOT/bin/coin_library.py" --status 2>&1 | tail -n "$N" ;;
  zig-refresh)     python3 "$ROOT/bin/zig_archive.py" 2>&1 | tail -n 10; python3 "$ROOT/bin/zig_archive.py" --status 2>&1 | tail -n 5 ;;
  call-lab-log)    tail -n "$N" "$ROOT/logs/calls_lab.txt" 2>/dev/null || echo "no calls lab output yet" ;;
  bee-study)       setsid nohup nice -n 15 python3 -u "$ROOT/research/mm_bee_multi.py" > "$ROOT/logs/bee_study.txt" 2>&1 < /dev/null &
                   echo "MM-Bee multi-coin study started in the background; read it with bee-study-log" ;;
  bee-tune)        pf="$ROOT/logs/bee_tune.pid"
                   if [ -f "$pf" ] && kill -0 "$(cat "$pf")" 2>/dev/null; then echo "a tuning run is already going (pid $(cat "$pf")); bee-tune-stop first"; exit 0; fi
                   setsid nohup nice -n 15 python3 -u "$ROOT/research/mm_bee_tune.py" > "$ROOT/logs/bee_tune.txt" 2>&1 < /dev/null &
                   echo $! > "$pf"
                   echo "MM-Bee tuning study started in the background (pid $!); read it with bee-tune-log" ;;
  bee-tune-stop)   pf="$ROOT/logs/bee_tune.pid"; if [ -f "$pf" ] && kill -- -"$(cat "$pf")" 2>/dev/null; then echo "MM-Bee tuning study stopped"; else echo "no MM-Bee tuning study running"; fi; rm -f "$pf" ;;
  bee-tune-log)    tail -n "$N" "$ROOT/logs/bee_tune.txt" 2>/dev/null || echo "no MM-Bee tuning output yet" ;;
  bee-study-log)   tail -n "$N" "$ROOT/logs/bee_study.txt" 2>/dev/null || echo "no MM-Bee study output yet" ;;
  *) echo "unknown command: $CMD"; echo "commands: status logs deploy-log mm-log health run-sync deploy rollback mm-start mm-stop mm-paper-start mm-paper-stop mm-paper-report mm-paper-data mm-paper-log trading-stop trading-resume carry-study shortlist-study calls-study study-log bee-study bee-study-log bee-tune bee-tune-stop bee-tune-log mm-preflight mm-plan mm-status live-drill world-probe world-refresh dns-check call-lab call-lab-log onchain-probe onchain-refresh strategy-lab strategy-lab-log zig-probe zig-status zig-refresh library-probe library-refresh library-status web-setup web-status publish-local publish-github ma-lab ma-lab-log root-key-saad root-keys root-login-keys root-login-off saad-user ssh-check token-check site-token-save"; exit 2 ;;
esac
