#!/bin/bash
# Serve beesells.xyz from this server (Caddy, automatic HTTPS), so the site does not depend on GitHub Pages.
# Idempotent: safe to run again. Run as user bees (sudo for the system parts) through the ops command "web-setup".
#
# What the web server can see: ONLY /var/www/beesells, a copy of the built site (no .git, no keys, no databases).
# The copy is refreshed every minute by bees-web-sync.timer from ~/BinanceData/site_build.
# Until DNS points beesells.xyz here, GitHub Pages keeps serving the site; Caddy just waits for its certificate.
set -euo pipefail
ROOT=/home/bees/BinanceData
WEB=/var/www/beesells
DOMAIN=beesells.xyz

echo "1/5 web server (Caddy, from the operating system's own packages)"
if ! command -v caddy >/dev/null; then
  sudo -n apt-get update -q >/dev/null
  sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -q caddy >/dev/null
fi
caddy version | head -1

echo "2/5 public copy of the site: $WEB"
sudo -n install -d -o bees -g bees -m 755 "$WEB"
"$ROOT/deploy/web_sync.sh" && echo "   copied: $(find "$WEB" -type f | wc -l) files"

echo "3/5 refresh the copy every minute (bees-web-sync.timer)"
sudo -n tee /etc/systemd/system/bees-web-sync.service >/dev/null <<EOF
[Unit]
Description=Copy the built site to $WEB
[Service]
Type=oneshot
User=bees
Nice=10
ExecStart=$ROOT/deploy/web_sync.sh
EOF
sudo -n tee /etc/systemd/system/bees-web-sync.timer >/dev/null <<'EOF'
[Unit]
Description=Copy the built site to the web folder every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
EOF
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now bees-web-sync.timer >/dev/null

echo "4/5 Caddy config (HTTPS for $DOMAIN, www redirects to it, a local check on 127.0.0.1:8088)"
sudo -n tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
	root * $WEB
	encode zstd gzip
	@hidden path /.git /.git/*
	respond @hidden 404
	file_server
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "strict-origin-when-cross-origin"
		Permissions-Policy "camera=(), microphone=(), geolocation=()"
		-Server
	}
	header /data/* Cache-Control "no-cache"
	header /sw.js Cache-Control "no-cache"
	header /index.html Cache-Control "no-cache"
}
www.$DOMAIN {
	redir https://$DOMAIN{uri} permanent
}
http://127.0.0.1:8088 {
	root * $WEB
	file_server
}
EOF
sudo -n caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1
sudo -n systemctl enable caddy >/dev/null 2>&1 || true
sudo -n systemctl restart caddy
sleep 2

echo "5/5 firewall: open 80 (certificate check + redirect) and 443 (the site)"
sudo -n ufw allow 80/tcp >/dev/null
sudo -n ufw allow 443/tcp >/dev/null
sudo -n ufw status | grep -E "^(22|80|443)" || true

echo "check: $(curl -s -o /dev/null -w 'local site HTTP %{http_code}' http://127.0.0.1:8088/)"
echo "done. Next: point $DOMAIN (A record) to this server; Caddy then gets the HTTPS certificate by itself."
