package gateway

import "time"

// Wire models. gateway/loft_gateway/api.py + service.py are the source of truth; the
// shapes match LoftCore Models.swift.

// Item is a file or folder in the user's Loft root.
type Item struct {
	ID          string  `json:"id"`
	ParentID    *string `json:"parent_id"`
	Name        string  `json:"name"`
	Type        string  `json:"type"` // "file" | "dir"
	Size        int64   `json:"size"`
	MtimeNs     int64   `json:"mtime_ns"`
	ContentEtag string  `json:"content_etag"`
	MetaEtag    string  `json:"meta_etag"`
	SHA256      *string `json:"sha256"`
	Fixed       bool    `json:"fixed"`
}

func (i *Item) IsDir() bool { return i.Type == "dir" }

// Parent returns the parent id, "" for the root.
func (i *Item) Parent() string {
	if i.ParentID == nil {
		return ""
	}
	return *i.ParentID
}

func (i *Item) Modified() time.Time { return time.Unix(0, i.MtimeNs) }

type ChildrenPage struct {
	Items      []Item  `json:"items"`
	NextCursor *string `json:"next_cursor"`
}

type ChangesPage struct {
	Updated []Item   `json:"updated"`
	Deleted []string `json:"deleted"`
	Anchor  string   `json:"anchor"`
	More    bool     `json:"more"`
}

func (c *ChangesPage) Empty() bool { return len(c.Updated) == 0 && len(c.Deleted) == 0 }

type LoginResult struct {
	DeviceID  string  `json:"device_id"`
	Token     string  `json:"token"`
	NASUser   string  `json:"nas_user,omitempty"`
	ExpiresAt float64 `json:"expires_at"`
}

type RootInfo struct {
	ID         string `json:"id"`
	Share      string `json:"share"`
	Path       string `json:"path"`
	RootItemID string `json:"root_item_id"`
	Anchor     string `json:"anchor"`
}

// AllShares is the share value that makes every share a top-level folder.
const AllShares = "*"

func (r *RootInfo) Display() string {
	if r.Share == AllShares {
		return "all shares"
	}
	if r.Path == "" {
		return r.Share
	}
	return r.Share + "/" + r.Path
}

type Me struct {
	NASUser    string    `json:"nas_user"`
	DeviceID   string    `json:"device_id"`
	DeviceName string    `json:"device_name"`
	Route      string    `json:"route"`
	Root       *RootInfo `json:"root"`
}

type Device struct {
	ID         string   `json:"id"`
	DeviceName string   `json:"device_name"`
	CreatedAt  float64  `json:"created_at"`
	LastUsedAt *float64 `json:"last_used_at"`
	RevokedAt  *float64 `json:"revoked_at"`
}

type DevicesResponse struct {
	Devices []Device `json:"devices"`
	Current string   `json:"current"`
}

type SharesResponse struct {
	Shares []string `json:"shares"`
}

type FoldersResponse struct {
	Share   string   `json:"share"`
	Path    string   `json:"path"`
	Folders []string `json:"folders"`
}

type UploadState struct {
	UploadID string `json:"upload_id"`
	Received int64  `json:"received"`
	Size     int64  `json:"size"`
	ChunkMax *int64 `json:"chunk_max,omitempty"`
}

// CommitRequest: create (ParentID+Name), modify (ItemID+IfMatch), or a safe-save
// successor (create + Replaces).
type CommitRequest struct {
	ParentID string `json:"parent_id,omitempty"`
	Name     string `json:"name,omitempty"`
	ItemID   string `json:"item_id,omitempty"`
	IfMatch  string `json:"if_match,omitempty"`
	Replaces string `json:"replaces,omitempty"`
}

// Commit results.
const (
	ResultCreated  = "created"
	ResultModified = "modified"
	ResultReplaced = "replaced"
	ResultConflict = "conflict"
)

type CommitResponse struct {
	Result     string  `json:"result"`
	Item       Item    `json:"item"`
	ReplacedID *string `json:"replaced_id"`
}

// DeleteResponse: "trashed" | "superseded" | "already_deleted"; all three are success.
type DeleteResponse struct {
	Result       string  `json:"result"`
	SupersededBy *string `json:"superseded_by"`
}

type Health struct {
	OK      bool   `json:"ok"`
	Service string `json:"service"`
	Route   string `json:"route"`
	Version string `json:"version"`
}

type Identity struct {
	DirectSPKISHA256 []string `json:"direct_spki_sha256"`
}

// Token is the device token (the only secret on this machine; never the password).
type Token struct {
	Token     string    `json:"token"`
	DeviceID  string    `json:"device_id"`
	IssuedAt  time.Time `json:"issued_at"`
	ExpiresAt time.Time `json:"expires_at"`
}

// RotateAfter matches the reference client: rotate weekly (gateway TTL is 90 days, sliding).
const RotateAfter = 7 * 24 * time.Hour

func TokenFrom(r *LoginResult) *Token {
	return &Token{Token: r.Token, DeviceID: r.DeviceID, IssuedAt: time.Now(),
		ExpiresAt: time.Unix(int64(r.ExpiresAt), 0)}
}

func (t *Token) DueForRotation() bool { return time.Since(t.IssuedAt) > RotateAfter }
