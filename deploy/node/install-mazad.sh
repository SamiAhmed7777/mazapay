#!/usr/bin/env bash
# Install the canonical MazaCoin Core v0.16.3 daemon for the MazaPay bridge.
# Idempotent: safe to re-run. Run as root:  sudo bash install-mazad.sh
# (also executable: ./install-mazad.sh). No secrets in this file - the RPC
# password is generated at install time and written 0600 into the data dir.
set -euo pipefail

# Resolve the script's own directory BEFORE any cd, so the bundled
# mazad.service is found regardless of invocation path (round-17 fix:
# dirname $0 evaluated after cd broke relative invocation and silently
# skipped unit installation).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MAZA_VER="0.16.3"
TARBALL="maza-${MAZA_VER}-x86_64-linux-gnu.tar.gz"
URL="https://github.com/MazaCoin/maza/releases/download/v${MAZA_VER}/${TARBALL}"
# Checksum PROVENANCE (trust-on-first-use pin, not an authoritative upstream
# checksum): the tarball was fetched from the official GitHub release URL at
# 2026-11-15 03:28 samihost local time (= 02:28 UTC) and this hash was
# observed at download. The release publishes NO SHA256SUMS asset (verified
# against the GitHub release-asset listing), so there is no authoritative
# value to compare against; anyone can reproduce the hash by re-downloading
# from the same URL. The installer fails hard on any mismatch.
EXPECTED_SHA256="57ef923ecc518119bf74681a932e250fd87035e8c717f95781df1d0d303ddf48"

# 0. prerequisites (round-17 polish)
for cmd in curl sha256sum openssl tar useradd systemctl install cut; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "FATAL: missing prerequisite: $cmd" >&2; exit 1; }
done
[ -f "${SCRIPT_DIR}/mazad.service" ] || { echo "FATAL: mazad.service not found next to installer (${SCRIPT_DIR})" >&2; exit 1; }

# 1. tarball present + checksum-verified
mkdir -p /opt/maza
cd /opt/maza
if [ ! -f "${TARBALL}" ]; then
  curl -fsSL -m 600 -o "${TARBALL}" "${URL}"
fi
ACTUAL_SHA256="$(sha256sum "${TARBALL}" | cut -d' ' -f1)"
if [ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]; then
  echo "FATAL: sha256 mismatch for ${TARBALL}" >&2
  echo "  expected: ${EXPECTED_SHA256}" >&2
  echo "  actual:   ${ACTUAL_SHA256}" >&2
  exit 1
fi

# 2. binaries extracted
if [ ! -x "maza-${MAZA_VER}/bin/mazad" ]; then
  tar xzf "${TARBALL}"
fi
chown -R root:root "maza-${MAZA_VER}"
chmod -R go-w "maza-${MAZA_VER}"

# 3. dedicated unprivileged service user
if ! id mazad >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin mazad
fi

# 4. data dir + config (generated once; rerun keeps existing credentials).
# NOTE: no walletnotify hook - the MazaPay watcher polls listtransactions on
# its own interval (MAZAPAY_WATCH_INTERVAL, default 10s); a discarded-output
# hook would be dead weight (round-17 fix: removed).
install -d -o mazad -g mazad -m 0750 /var/lib/mazad
if [ ! -f /var/lib/mazad/maza.conf ]; then
  RPCPASS="$(openssl rand -hex 24)"
  install -o mazad -g mazad -m 0600 /dev/null /var/lib/mazad/maza.conf
  {
    echo "server=1"
    echo "daemon=0"
    echo "printtoconsole=1"
    echo "rpcbind=127.0.0.1"
    echo "rpcallowip=127.0.0.1"
    echo "rpcport=12832"
    echo "rpcuser=mazapay"
    echo "rpcpassword=${RPCPASS}"
  } > /var/lib/mazad/maza.conf
fi
chown mazad:mazad /var/lib/mazad/maza.conf
chmod 0600 /var/lib/mazad/maza.conf

# 5. systemd unit (required - fails hard if missing, never silently skipped)
install -m 0644 "${SCRIPT_DIR}/mazad.service" /etc/systemd/system/mazad.service
systemctl daemon-reload
systemctl enable mazad.service

echo "install complete. start with: systemctl start mazad"
echo "verify with: sudo -u mazad /opt/maza/maza-${MAZA_VER}/bin/maza-cli -datadir=/var/lib/mazad getblockchaininfo"
