#!/bin/bash
# One-time server setup, run as user "bees" on a server prepared by cloud-init.yaml or bootstrap.sh:
#
#   git clone https://github.com/beeelahi/bees-engine.git ~/app && bash ~/app/deploy/install_server.sh [ops_key.pub]
#
# 1. installs the systemd units (engine every 15 minutes, deployment check every 5 minutes, market maker),
# 2. creates "opsbot": a user whose SSH key can only run the fixed operations list in deploy/ops.sh,
#    installs ops_key.pub for it if given (the PRIVATE key goes only into the bees-ops repository's secrets),
# 3. deploys the current main branch (after its GitHub checks and the tests pass on this server).
set -euo pipefail
[ "$(id -un)" = "bees" ] || { echo "run as user bees"; exit 1; }
APP=/home/bees/app; ROOT=/home/bees/BinanceData
mkdir -p "$ROOT/logs" "$ROOT/data" /home/bees/releases
chmod 700 "$ROOT"

echo "1/3 systemd units"
for u in "$APP"/deploy/systemd/*; do sudo install -m 644 "$u" /etc/systemd/system/; done
sudo systemctl daemon-reload
sudo systemctl enable --now bees-sync.timer bees-deploy.timer
sudo systemctl enable bees-mm.service >/dev/null 2>&1 || true      # idles until mm.json enables it

echo "2/3 operations user (restricted)"
id opsbot >/dev/null 2>&1 || sudo useradd -m -s /bin/bash opsbot
sudo passwd -l opsbot >/dev/null
sudo install -m 755 -o root -g root /dev/stdin /usr/local/bin/bees-ops <<'EOF'
#!/bin/bash
# Forced command for opsbot's SSH key: only "word [number]" from the ops list reaches ops.sh, as user bees.
set -euo pipefail
read -r -a A <<< "${SSH_ORIGINAL_COMMAND:-status}"
[[ "${A[0]:-status}" =~ ^[a-z-]{2,20}$ ]] || { echo "rejected"; exit 2; }
[[ -z "${A[1]:-}" || "${A[1]}" =~ ^[0-9]{1,4}$ ]] || { echo "rejected"; exit 2; }
exec sudo -n -u bees /home/bees/BinanceData/deploy/ops.sh "${A[0]:-status}" ${A[1]:-}
EOF
echo "opsbot ALL=(bees) NOPASSWD: /home/bees/BinanceData/deploy/ops.sh" | sudo tee /etc/sudoers.d/91-opsbot >/dev/null
sudo chmod 440 /etc/sudoers.d/91-opsbot && sudo visudo -cq
sudo sed -i 's/^AllowUsers bees$/AllowUsers bees opsbot/' /etc/ssh/sshd_config.d/00-bees.conf 2>/dev/null || true
if [ -n "${1:-}" ] && [ -f "$1" ]; then
  key=$(head -1 "$1")
  [[ "$key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo "ops key must be one ssh-ed25519 public key"; exit 1; }
  sudo mkdir -p /home/opsbot/.ssh
  echo "restrict,command=\"/usr/local/bin/bees-ops\" $key" | sudo tee /home/opsbot/.ssh/authorized_keys >/dev/null
  sudo chown -R opsbot:opsbot /home/opsbot/.ssh && sudo chmod 700 /home/opsbot/.ssh && sudo chmod 600 /home/opsbot/.ssh/authorized_keys
  echo "   ops key installed (restricted to the ops command list)"
fi
sudo sshd -t && sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd

echo "3/3 first deployment"
bash "$APP/deploy/pull_deploy.sh" --now || true
echo "Deployed: $(cut -c1-7 "$ROOT/.deployed_sha" 2>/dev/null || echo 'not yet - checks still running on GitHub; the timer retries every 5 minutes')"
systemctl list-timers bees-sync.timer bees-deploy.timer --no-pager | head -4
