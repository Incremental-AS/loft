package vfs

import (
	"os"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// Mount mounts the file system at dir. The caller runs Start and waits on the server.
func Mount(dir string, f *FS, debug bool) (*fuse.Server, error) {
	attr := time.Second
	entry := time.Second
	root := f.RootNode()
	f.mu.Lock()
	f.inodes[f.rootID] = root.EmbeddedInode()
	f.mu.Unlock()
	opts := &fs.Options{
		MountOptions: fuse.MountOptions{
			FsName:        "loft",
			Name:          "loft",
			Debug:         debug,
			MaxWrite:      1 << 20,
			MaxReadAhead:  1 << 20,
			DisableXAttrs: false,
		},
		EntryTimeout: &entry,
		AttrTimeout:  &attr,
		UID:          uint32(os.Getuid()),
		GID:          uint32(os.Getgid()),
	}
	return fs.Mount(dir, root, opts)
}
