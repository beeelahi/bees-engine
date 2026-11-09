#!/bin/bash
# Serve beesells.xyz from this server (Caddy, automatic HTTPS), so the site does not depend on GitHub Pages.
# Idempotent: safe to run again. Run as user bees (sudo for the system parts) through the ops command "web-setup".
#
# What the web server can see: ONLY /var/www/beesells, a copy of the built site (no .git, no keys, no databases).
# The copy is refreshed every minute by bees-web-sync.timer from ~/BinanceData/site_build.
# Until DNS points beesells.xyz here, GitHub Pages keeps serving the site; Caddy just waits for its certificate.
set -euo pipefail
cd /
ROOT=/home/bees/BinanceData
WEB=/var/www/beesells
DOMAIN=beesells.xyz

echo "1/5 web server (Caddy, current release from Caddy's official signed apt repository; security updates come with apt)"
KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
if [ ! -f /etc/apt/sources.list.d/caddy-stable.list ]; then
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | sudo -n gpg --batch --yes --dearmor -o "$KEYRING"
  echo "deb [signed-by=$KEYRING] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" | sudo -n tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
fi
sudo -n apt-get update -q >/dev/null 2>&1
sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -q caddy >/dev/null 2>&1
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

echo "4/5 Caddy config"
# Ask for the HTTPS certificate only once the domain points here: failed tries count against Let's Encrypt's
# limits (about 5 an hour) and would delay the certificate after the switch.
MYIP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk "{for(i=1;i<=NF;i++) if(\$i==\"src\") print \$(i+1)}")
sudo -n resolvectl flush-caches 2>/dev/null || true
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
if [ -n "$MYIP" ] && [ "$DNSIP" = "$MYIP" ]; then
  echo "   $DOMAIN points here ($MYIP): HTTPS on"
  PUBLIC=1
else
  echo "   $DOMAIN points to ${DNSIP:-nothing}, this server is ${MYIP:-unknown}: HTTPS waits (run web-setup again after the DNS change)"
  PUBLIC=0
fi
if [ "$PUBLIC" = 1 ]; then
sudo -n tee /etc/caddy/Caddyfile >/dev/null <<EOF
{
	acme_ca https://acme-v02.api.letsencrypt.org/directory
}
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
else
sudo -n tee /etc/caddy/Caddyfile >/dev/null <<EOF
http://127.0.0.1:8088 {
	root * $WEB
	file_server
}
EOF
fi
sudo -n caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1
sudo -n systemctl enable caddy >/dev/null 2>&1 || true
sudo -n systemctl restart caddy
sleep 2

echo "5/5 firewall: open 80 (certificate check + redirect) and 443 (the site)"
sudo -n ufw allow 80/tcp >/dev/null
sudo -n ufw allow 443/tcp >/dev/null
sudo -n ufw status | grep -E "^(22|80|443)" || true

echo "check: $(curl -s -o /dev/null -w 'local site HTTP %{http_code}' http://127.0.0.1:8088/) | $(ls -ld "$WEB" "$WEB/index.html" 2>&1 | awk '{print $1, $NF}' | tr '\n' ' ')"
echo "done. Next: point $DOMAIN (A record) to this server; Caddy then gets the HTTPS certificate by itself."
