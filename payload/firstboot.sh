#!/bin/sh
# First-boot setup for a DMRHost hotspot, written to the card by dmrhost-installer.
#
#   firstboot.sh --stage   run once by cloud-init: move the setup off the boot
#                          partition (it is FAT, so anyone can read it) and start
#                          dmrhost-firstboot.service
#   firstboot.sh           run by that service: install and configure everything.
#                          It retries until it succeeds, then disables itself.
#
# Progress goes to /var/log/dmrhost-firstboot.log.
set -eu

BOOT=/boot/firmware
STAGE=/var/lib/dmrhost-setup
LOG=/var/log/dmrhost-firstboot.log
UNIT=/etc/systemd/system/dmrhost-firstboot.service

exec >>"$LOG" 2>&1
say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

if [ "${1:-}" = --stage ]; then
  say "staging $BOOT/dmrhost-setup to $STAGE"
  install -d -m 0700 "$STAGE"
  cp -a "$BOOT/dmrhost-setup/." "$STAGE/"
  chmod -R go-rwx "$STAGE"
  install -m 0644 "$STAGE/dmrhost-firstboot.service" "$UNIT"
  rm -rf "$BOOT/dmrhost-setup"
  # cloud-init only applies network-config on the first boot; the Wi-Fi password
  # now lives in /etc/netplan (root only), so drop the copy on the FAT partition.
  printf '# Applied on first boot by dmrhost-installer.\n# The live network settings are in /etc/netplan.\n' > "$BOOT/network-config"
  sync
  systemctl daemon-reload
  systemctl enable dmrhost-firstboot.service
  systemctl start --no-block dmrhost-firstboot.service
  exit 0
fi

# shellcheck source=/dev/null
. "$STAGE/setup.env"
say "first-boot setup for ${CALLSIGN:-?} starting"

# --- wait for the network and the clock (no RTC: apt signatures need the time)
i=0
until getent hosts repo.test.net.in >/dev/null 2>&1; do
  i=$((i + 1))
  [ $i -le 120 ] || { say "no network after 10 minutes, will retry"; exit 1; }
  sleep 5
done
i=0
until [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; do
  i=$((i + 1))
  [ $i -le 24 ] || { say "clock not synchronised yet, carrying on"; break; }
  sleep 5
done

# --- free the GPIO UART for the hat (no login console, no Bluetooth on it)
systemctl disable --now serial-getty@ttyAMA0.service hciuart.service >/dev/null 2>&1 || true

# --- DMRHost (and DisplayServer for the OLED) from the DMRHost apt repo
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://repo.test.net.in/dmrhost/public.key -o /etc/apt/keyrings/dmrhost.asc
cat > /etc/apt/sources.list.d/dmrhost.sources <<'EOF'
Types: deb
URIs: https://repo.test.net.in/dmrhost
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/dmrhost.asc
EOF
export DEBIAN_FRONTEND=noninteractive
apt-get update
PKGS=dmrhost
[ "$OLED" = 1 ] && PKGS="$PKGS displayserver"
# Don't let the packages start their services: the stock MMDVM.ini (Duplex=1, TX == RX)
# makes DMRHost crash-loop until ours goes in below. We start them at the end.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT
# shellcheck disable=SC2086
apt-get install -y -o Dpkg::Options::=--force-confold $PKGS
rm -f /usr/sbin/policy-rc.d
say "installed $PKGS"

# --- our hotspot config (keep the packaged one for reference)
[ -f /etc/MMDVM.ini.dist ] || cp -p /etc/MMDVM.ini /etc/MMDVM.ini.dist
install -m 0600 "$STAGE/MMDVM.ini" /etc/MMDVM.ini

# --- radioid.net users, trimmed to ID,callsign,name: DisplayServer and the
# dashboard use it for names, and the full file costs DisplayServer ~85 MB RAM
cat > /etc/cron.weekly/radioid-users <<'EOF'
#!/bin/sh
# Weekly radioid.net refresh, trimmed to the columns the OLED and dashboard show (saves RAM)
curl -fsSL https://radioid.net/static/user.csv | cut -d, -f1-3 > /usr/local/etc/user.csv.new && mv /usr/local/etc/user.csv.new /usr/local/etc/user.csv
EOF
chmod 0755 /etc/cron.weekly/radioid-users
install -d -m 0755 /usr/local/etc
/etc/cron.weekly/radioid-users || say "radioid.net download failed; the weekly job will try again"

# --- OLED: DisplayServer draws nothing unless i2c-dev is loaded before it starts
if [ "$OLED" = 1 ]; then
  echo i2c-dev > /etc/modules-load.d/i2c-dev.conf
  modprobe i2c-dev
fi

# --- dmrdash
if [ "$DASHBOARD" = 1 ]; then
  DEB=$(find "$STAGE" -maxdepth 1 -name 'dmrdash_*_all.deb' | sort | tail -n 1)
  if [ -z "$DEB" ]; then
    URL=$(curl -fsSL https://api.github.com/repos/jcalado/dmrdash/releases/latest 2>/dev/null |
      python3 -I -c 'import json, sys; print(next(a["browser_download_url"] for a in json.load(sys.stdin)["assets"] if a["name"].endswith("_all.deb")))' 2>/dev/null) || URL=
    if [ -n "$URL" ] && curl -fsSL "$URL" -o "$STAGE/$(basename "$URL")"; then
      DEB="$STAGE/$(basename "$URL")"
    fi
  fi
  if [ -n "$DEB" ]; then
    apt-get install -y "$DEB"
    install -m 0640 -o root -g dmrdash "$STAGE/dmrdash.conf" /etc/dmrdash.conf
    systemctl restart dmrdash
    say "installed $(basename "$DEB")"
  else
    # don't hold the hotspot hostage to the dashboard; install.sh can add it later
    say "no dmrdash package on the card and none downloadable; skipped the dashboard"
  fi
fi

# --- login password, if one was chosen (the image only has the SSH key)
if [ -f "$STAGE/login" ]; then
  chpasswd < "$STAGE/login"
  say "password set for ${LOGIN_USER:-?}"
fi

systemctl enable dmrhost >/dev/null
systemctl restart dmrhost
if [ "$OLED" = 1 ]; then
  systemctl enable displayserver >/dev/null
  systemctl restart displayserver
fi

# --- done: drop the secrets and stop running at boot
rm -f "$STAGE/MMDVM.ini" "$STAGE/dmrdash.conf" "$STAGE/login" "$STAGE"/dmrdash_*_all.deb
touch "$STAGE/done"
systemctl disable dmrhost-firstboot.service
say "first-boot setup finished"
