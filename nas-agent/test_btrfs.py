#!/usr/bin/env python3
"""The agent against a real btrfs: what it reports for each kind of change.

    sudo python3 test_btrfs.py <empty directory on a btrfs filesystem, or a loop mount>

e.g.  truncate -s 512M /var/tmp/b.img && mkfs.btrfs -q /var/tmp/b.img
      mkdir -p /var/tmp/b && mount -o loop,strictatime /var/tmp/b.img /var/tmp/b
      python3 test_btrfs.py /var/tmp/b ; umount /var/tmp/b ; rm /var/tmp/b.img

Needs root (btrfs tree search). Makes two subvolumes in the directory, deletes them
afterwards. A stand-in gateway keeps the cursor and records what is reported.
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loft_nas_agent as A  # noqa: E402


class StandInGateway:
    def __init__(self):
        self.cur, self.got, self.uuid, self.synced = {}, [], {}, {}

    def post(self, path, body):
        if path.endswith("/sync"):
            self.synced = {sh["id"]: sh for sh in body["shares"]}
            for sh in body["shares"]:
                if self.uuid.get(sh["id"]) not in (None, sh["uuid"]):
                    self.got.append("<recreated>")
                    self.cur.pop(sh["id"], None)
                self.uuid[sh["id"]] = sh["uuid"]
                self.cur.setdefault(sh["id"], sh["generation"])
            return {"cursors": {k: v for k, v in self.cur.items() if k in self.synced}}
        assert body["since"] == self.cur[body["id"]], (body, self.cur)
        self.got += body["dirs"] + (["<rescan>"] if body.get("overflow") else [])
        if body["final"]:
            self.cur[body["id"]] = body["until"]
        return {"ok": True}


def main(top):
    sub = [os.path.join(top, "share"), os.path.join(top, "personal")]
    late = os.path.join(top, "late")
    for s in sub:
        subprocess.run(["btrfs", "subvolume", "create", s], check=True, capture_output=True)
        os.makedirs(f"{s}/.data", exist_ok=True)
    d = f"{sub[0]}/.data"
    os.makedirs(f"{d}/a/b")
    os.makedirs(f"{d}/c")
    os.makedirs(f"{sub[1]}/.data/Privat")
    open(f"{d}/c/old", "w").write("o")
    subprocess.run(["sync"])
    time.sleep(3)                       # c/old's ctime well before the first cursor
    A.CTIME_SLACK_S = 1
    cfg = A.Config(gateway="https://x:1", pins=["x"], key_file="/dev/null", smb_conf="",
                   shares=[A.Share("S", d), A.Share("P", f"{sub[1]}/.data", "alice"),
                           A.Share("L", f"{late}/.data")],
                   max_dirs=3000, chunk=100)
    gw = StandInGateway()
    ag = A.Agent(cfg, gw=gw)
    ag.discover()
    ag.sync()
    fails = 0

    def step(label, fn=None, commit=True, expect=None, resync=False):
        nonlocal fails
        if fn:
            fn()
        if commit:
            subprocess.run(["btrfs", "filesystem", "sync", top], capture_output=True)
        gw.got = []
        if resync:                      # what run() does next: discover and sync again
            ag.round()
            ag.discover()
            ag.sync()
        ag.round()
        got = sorted(set(gw.got))
        ok = expect is None or got == sorted(expect)
        fails += not ok
        print("ok  " if ok else "FAIL", label, got if len(got) < 12 else f"{len(got)} folders")

    def w(p, data="x"):
        with open(p, "a") as f:
            f.write(data)

    def many(n, base):
        for i in range(n):
            os.makedirs(f"{d}/{base}/d{i}", exist_ok=True)

    step("first check: the last transaction again (harmless)")
    step("nothing", expect=[])
    step("new file, transaction still open", lambda: w(f"{d}/a/b/f1"), commit=False, expect=["a/b"])
    step("second new file, same open transaction", lambda: w(f"{d}/a/b/f2"), commit=False,
         expect=["a/b"])
    step("the commit (may repeat a/b)")
    step("nothing", expect=[])
    step("mkdir", lambda: os.mkdir(f"{d}/a/new"), expect=["a", "a/new"])
    step("append in place", lambda: w(f"{d}/a/b/f1", "more"), expect=["a/b"])
    step("read (atime only)", lambda: open(f"{d}/a/b/f1").read(), expect=[])
    step("read an old file (atime only)", lambda: open(f"{d}/c/old").read(), expect=[])
    step("mtime set to 2001", lambda: os.utime(f"{d}/a/b/f1", (1e9, 1e9)), expect=["a/b"])
    # F1 of review_maja_ba880e0: a file reported while still being written, finished in
    # the same open transaction (btrfs keeps the inode update in memory until the commit)
    half = {}

    def half_write():
        half["f"] = open(f"{d}/a/b/f3", "wb")
        half["f"].write(b"x" * 4096)
        half["f"].flush()

    def finish():
        half["f"].write(b"y" * 2482)
        half["f"].close()
        t = time.time_ns() + 10_000_000            # Samba sets the write time at close
        os.utime(f"{d}/a/b/f3", ns=(t, t))
    step("half written, transaction open", half_write, commit=False, expect=["a/b"])
    step("rest of it + write time, same transaction", finish, commit=False)
    step("its commit: reported again", expect=["a/b"])
    step("delete", lambda: os.remove(f"{d}/a/b/f2"), expect=["a/b"])
    step("move between folders", lambda: os.rename(f"{d}/a/b/f1", f"{d}/c/f1"), expect=["a/b", "c"])
    step("outside the share root", lambda: w(f"{sub[0]}/outside.txt"), expect=[])
    step("file at the share root", lambda: w(f"{d}/top.txt"), expect=[""])
    step("per-account share", lambda: w(f"{sub[1]}/.data/Privat/x.mobileconfig"), expect=["Privat"])
    step("150 new folders, in chunks of 100", lambda: many(150, "many"))
    step("more than max_dirs: rescan", lambda: many(3100, "flood"), expect=["<rescan>"])
    step("nothing", expect=[])

    def deep():                         # a path longer than INO_LOOKUP can return
        fd = os.open(d, os.O_RDONLY)
        for _ in range(17):
            os.mkdir("x" * 250, dir_fd=fd)
            nfd = os.open("x" * 250, os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = nfd
        os.close(os.open("f", os.O_CREAT | os.O_WRONLY, dir_fd=fd))
        os.close(fd)
        w(f"{sub[1]}/.data/Privat/y")
    step("a path too long to look up: that share is rescanned, the other reports",
         deep, expect=["<rescan>", "Privat"])
    step("nothing", expect=[])
    shutil.rmtree(f"{d}/{'x' * 250}")
    step("(removing it)")

    def recreate():
        subprocess.run(["btrfs", "subvolume", "delete", sub[1]], check=True, capture_output=True)
        subprocess.run(["btrfs", "subvolume", "create", sub[1]], check=True, capture_output=True)
        os.makedirs(f"{sub[1]}/.data/Privat")
    step("a share deleted and recreated at its path: reopened, the gateway told",
         recreate, expect=["<recreated>", "", "Privat"], resync=True)   # + its last transaction
    fails += not gw.synced["S"]["ok"] or "P\nalice" not in gw.synced
    step("and it reports again", lambda: w(f"{sub[1]}/.data/Privat/z"), expect=["Privat"])

    def appear():
        subprocess.run(["btrfs", "subvolume", "create", late], check=True, capture_output=True)
        os.makedirs(f"{late}/.data/x")
    fails += "L" in ag.watched
    step("a share missing at start is watched once it is there", appear, expect=["", "x"],
         resync=True)                   # its first transaction, as for every new cursor
    fails += "L" not in ag.watched
    step("and reports", lambda: w(f"{late}/.data/x/f"), expect=["x"])
    print("ctime filter:", {k: w_.atime_writes for k, w_ in ag.watched.items()})
    for w_ in ag.watched.values():
        w_.close()
    for s in sub + [late]:
        subprocess.run(["btrfs", "subvolume", "delete", s], capture_output=True)
        shutil.rmtree(s, ignore_errors=True)
    print("ALL OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
