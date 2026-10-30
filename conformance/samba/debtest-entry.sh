#!/usr/bin/env bash
# The installed .deb, set up the documented way (`loft-gateway init`) and run as the
# package's `loft` user. No systemd in a container: the master key is a 0600 file and
# thumbnails use a worker subprocess instead of loft-thumb.socket.
set -euo pipefail
if [ ! -f /etc/loft/gateway.toml ]; then
  loft-gateway init -y --config /etc/loft/gateway.toml \
    --smb-server samba --smb-encryption "${LOFT_SMB_ENCRYPTION:-required}" \
    --name "Deb test" --lan-url https://gateway:8443 \
    --public-mode "${LOFT_PUBLIC_MODE:-tunnel}" --public-url "${LOFT_PUBLIC_URL:-https://loft.example.invalid}" \
    --public-host 0.0.0.0 --master-key file --thumb-socket "" --no-qr
fi
exec runuser -u loft -- loft-gateway --config /etc/loft/gateway.toml
