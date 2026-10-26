#!/bin/bash
# Applies the same setup as cloud-init.yaml to a server created without it. Run as root, once:
#   scp ~/BinanceData/cloud/bootstrap.sh root@IP:  &&  ssh root@IP bash bootstrap.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
id bees >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo bees
passwd -l bees >/dev/null
echo 'bees ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-bees && chmod 440 /etc/sudoers.d/90-bees
apt-get update -q && apt-get -y -q upgrade && apt-get install -y -q python3 git curl sqlite3 rsync ufw fail2ban unattended-upgrades
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-bees.conf <<'BEES_EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers bees
MaxAuthTries 3
X11Forwarding no
BEES_EOF
mkdir -p /etc/apt/apt.conf.d
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'BEES_EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
BEES_EOF
mkdir -p /home/bees/.ssh && cp /root/.ssh/authorized_keys /home/bees/.ssh/authorized_keys
chown -R bees:bees /home/bees/.ssh && chmod 700 /home/bees/.ssh && chmod 600 /home/bees/.ssh/authorized_keys
mkdir -p /home/bees/BinanceData && chown bees:bees /home/bees/BinanceData && chmod 700 /home/bees/BinanceData
ufw default deny incoming && ufw default allow outgoing && ufw allow 22/tcp && ufw --force enable
systemctl enable --now fail2ban
sshd -t && (systemctl restart ssh || systemctl restart sshd)
[ "$(sshd -T | awk '/^permitrootlogin/{print $2}')" = "no" ] || echo "WARNING: root login still allowed - tell Claude"
echo "Done. From now on log in as: ssh bees@$(hostname -I | awk '{print $1}')  (root login and passwords are off)"
