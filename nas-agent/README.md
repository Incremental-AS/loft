# Loft NAS agent

Optional. For NAS boxes whose shares live on **btrfs** (one subvolume per share, as
most current NAS systems do it).

Without it, the gateway learns about changes made on the NAS in two ways: SMB change
notify, which only reports what goes through Samba, and a periodic full rescan, which
takes over an hour on a large NAS. A file that reaches the disk any other way (the
NAS's own web or phone app, a job running on the NAS, NFS, rsync over ssh, a trash
restore) waits for that rescan.

The agent runs on the NAS and reads the filesystem's own record instead. btrfs stamps
every inode it writes with the id of the transaction that wrote it, so "every folder
written since transaction N" is complete whatever wrote it. It costs milliseconds
even on a share with millions of files (one tree search per share every 1.5 s). The
gateway re-reads the reported folders as each signed-in user. New files, new folders
and renames reach the Macs within about 2 seconds; deletes and in-place edits when
btrfs next commits, within about 30 seconds (see below). Once the agent covers a root,
the periodic full rescan stops.

## What it sends, and how

- **Folder paths only**, relative to the share root: a changed folder, or the parent
  folder of a changed file. Never file contents, never anything outside a share's
  root, never shares or folders listed in `exclude`.
- **It dials out; nothing listens on the NAS.** HTTPS to the gateway's *direct*
  listener, pinned to the gateway's key (the same SPKI pins the apps use). Each
  request is signed with a key the two share (HMAC-SHA256 over method, path,
  timestamp and body; timestamps strictly increase, so a request can't be replayed).
- **The gateway keeps the cursor** (the transaction each share was reported up to).
  The agent keeps nothing on disk: stopped for a day, it reports the whole day when
  it is back. A share whose subvolume was recreated is rescanned once.
- Reads only: the btrfs tree search and inode lookup ioctls need `CAP_SYS_ADMIN`.
  The systemd unit allows that capability and `CAP_DAC_READ_SEARCH`, nothing else,
  and makes the filesystem read-only. It also limits the agent to the gateway's
  address where the kernel supports that (cgroup-BPF); the agent itself only ever
  connects to the configured address, and only once the gateway's key matches a pin.
- **A share it can't read doesn't go unnoticed.** One failing share doesn't hold up
  the others; the agent tells the gateway which shares' rounds aren't going through,
  and the gateway counts a share as reported only while it hears of it (every 30 s).
  A share it can't name precisely (a path longer than the lookup can return) is
  rescanned. A share missing when the agent starts (volume not mounted yet) is retried
  every minute; one deleted and recreated at its path is reopened and rescanned.
  Any root with such a share is back on periodic full passes until it clears.
- Standard-library Python 3.11+, one file.

A read that only updates atime is not reported. On a volume mounted `noatime` reads
never rewrite an inode, so nothing needs filtering. Otherwise the agent skips inodes
whose ctime is older than the cursor (a change always sets ctime, which userspace
can't set back), with 5 minutes' slack for a slow commit. Deletes and in-place edits
show up when btrfs commits its transaction, every 30 s by default; new files, new
folders and renames show up at once.

## Install

On the NAS (paths are examples; keep them somewhere that survives the NAS's
firmware updates):

```sh
mkdir -p /data/loft-agent && chmod 700 /data/loft-agent
cp loft_nas_agent.py /data/loft-agent/
cp agent.toml.example /data/loft-agent/agent.toml        # edit: gateway, pins, shares
head -c 32 /dev/urandom | base64 > /data/loft-agent/agent.key
chmod 600 /data/loft-agent/agent.key /data/loft-agent/agent.toml
python3 /data/loft-agent/loft_nas_agent.py --print-changes 0 | head   # check the shares
cp loft-nas-agent.service /etc/systemd/system/
systemctl edit loft-nas-agent       # [Service] IPAddressAllow=<the gateway's address>
systemctl enable --now loft-nas-agent
```

On the gateway: put the same key in a file readable by the gateway only, set
`nas_agent_key_path` in `gateway.toml` and restart it. Its log then says
`NAS agent: reporting <share> from now on` for each share, and once a full pass
has run, `NAS agent: root ... covered: no more periodic full passes`.

**Shares whose path depends on the account** (`path = .../%U` in Samba, for example a
per-user "personal drive"): Samba's config can't say which folder belongs to whom, so
each account's folder is named in `agent.toml` (`[[share]]` with `user = ...`). The
gateway gives such a folder's changes only to that account.

## Test

```sh
sudo python3 test_btrfs.py <empty directory on btrfs>    # see its docstring for a loop mount
```

CI runs it on a loop-mounted btrfs (`.github/workflows/gateway.yml`). The gateway side
is in `gateway/loft_gateway/nas_agent.py` and `gateway/tests/test_nas_agent.py`.
