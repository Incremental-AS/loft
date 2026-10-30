#!/usr/bin/env bash
# Throwaway Samba for the conformance run. Accounts and passwords come from the
# environment (test values set in docker-compose.yml, never real ones).
#   SAMBA_ENCRYPT   server smb encrypt: desired | required | if_required | off
set -euo pipefail
: "${SMB_USER:?}" "${SMB_PASSWORD:?}" "${SMB_USER2:?}" "${SMB_PASSWORD2:?}"
ENC=${SAMBA_ENCRYPT:-desired}

groupadd -f loftusers
for pair in "$SMB_USER:$SMB_PASSWORD" "$SMB_USER2:$SMB_PASSWORD2"; do
  u=${pair%%:*}; p=${pair#*:}
  id "$u" >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin -G loftusers "$u"
  printf '%s\n%s\n' "$p" "$p" | smbpasswd -s -a "$u" >/dev/null
done
mkdir -p /srv/files /srv/media /srv/backup
chgrp loftusers /srv/files /srv/media /srv/backup
chmod 2770 /srv/files /srv/media /srv/backup

cat > /etc/samba/smb.conf <<EOF
[global]
  server role = standalone server
  workgroup = WORKGROUP
  server min protocol = SMB2_10
  server smb encrypt = $ENC
  map to guest = never
  restrict anonymous = 2
  access based share enum = yes
  load printers = no
  printing = bsd
  printcap name = /dev/null
  disable spoolss = yes
  log level = 1
  # Linux file systems are case-sensitive; Windows/NAS shares are not.
  case sensitive = no

[files]
  path = /srv/files
  read only = no
  valid users = $SMB_USER $SMB_USER2
  create mask = 0660
  directory mask = 2770
  force group = loftusers

[media]
  path = /srv/media
  read only = no
  valid users = $SMB_USER
  create mask = 0660
  directory mask = 2770
  force group = loftusers

[backup]
  path = /srv/backup
  read only = no
  valid users = $SMB_USER
EOF
testparm -s >/dev/null 2>&1 || { testparm -s; exit 1; }
exec smbd --foreground --no-process-group --debug-stdout
