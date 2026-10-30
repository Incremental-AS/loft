package gateway

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// ResumeStore remembers unfinished upload sessions by content ("<sha256>:<size>"), so a
// retried save continues from the gateway's `received` instead of zero. Content-keyed is
// safe: the gateway checks the whole-file hash at commit.
type ResumeStore interface {
	ResumableUpload(sha string, size int64) string
	SetResumableUpload(id, sha string, size int64) // id "" forgets
}

// HashFile returns the hex SHA-256 and size of a file.
func HashFile(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

func (c *Client) UploadCreate(ctx context.Context, size int64, sha, near string) (*UploadState, error) {
	b := map[string]any{"size": size, "sha256": sha}
	if near != "" {
		b["near_id"] = near
	}
	var out UploadState
	return &out, c.call(ctx, &out, request{method: "POST", path: "v1/uploads", body: jsonBody(b)})
}

func (c *Client) UploadStatus(ctx context.Context, id string) (*UploadState, error) {
	var out UploadState
	return &out, c.call(ctx, &out, request{method: "GET", path: "v1/uploads/" + url.PathEscape(id)})
}

func (c *Client) UploadChunk(ctx context.Context, id string, offset int64, data []byte) (*UploadState, error) {
	h := sha256.Sum256(data)
	var out UploadState
	return &out, c.call(ctx, &out, request{method: "PUT", path: "v1/uploads/" + url.PathEscape(id),
		query: url.Values{"offset": {strconv.FormatInt(offset, 10)}}, body: data,
		ctype: "application/octet-stream", headers: map[string]string{"X-Chunk-Sha256": hex.EncodeToString(h[:])},
		timeout: 5 * time.Minute})
}

// Commit places a finished upload. The gateway re-reads the whole file before it answers.
func (c *Client) Commit(ctx context.Context, uploadID string, req CommitRequest) (*CommitResponse, error) {
	var out CommitResponse
	return &out, c.call(ctx, &out, request{method: "POST", path: "v1/uploads/" + url.PathEscape(uploadID) + "/commit",
		body: jsonBody(req), timeout: 10 * time.Minute})
}

// Upload sends a local file to a finished, hash-checked upload session and returns its
// id. sha/size must describe the file's content (the caller hashed it, so it can tell
// whether the file changed since). Resumes from a remembered session, from the gateway's
// `received` after a 409 wrong_offset, and after transport errors.
func (c *Client) Upload(ctx context.Context, path, sha string, size int64, near string, rs ResumeStore,
	progress func(done, total int64)) (string, error) {
	var st *UploadState
	if rs != nil {
		if id := rs.ResumableUpload(sha, size); id != "" {
			if s, err := c.UploadStatus(ctx, id); err == nil {
				st = s
			}
		}
	}
	if st == nil {
		s, err := c.UploadCreate(ctx, size, sha, near)
		if err != nil {
			return "", err
		}
		st = s
	}
	if rs != nil {
		rs.SetResumableUpload(st.UploadID, sha, size)
	}
	chunk := c.ChunkLimit
	if st.ChunkMax != nil && *st.ChunkMax > 0 && *st.ChunkMax < chunk {
		chunk = *st.ChunkMax
	}
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	offset := st.Received
	failures := 0
	recreated := false
	buf := make([]byte, chunk)
	for offset < size {
		if err := ctx.Err(); err != nil {
			return "", err
		}
		n := min(chunk, size-offset)
		got, err := f.ReadAt(buf[:n], offset)
		if got == 0 && err != nil {
			return "", ErrChangedDuringTransfer // the file shrank under us
		}
		s, err := c.UploadChunk(ctx, st.UploadID, offset, buf[:got])
		switch {
		case err == nil:
			offset, failures = s.Received, 0
			if progress != nil {
				progress(offset, size)
			}
		case IsCode(err, "wrong_offset"):
			a, _ := AsAPI(err)
			if a.Received == nil {
				return "", err
			}
			offset = *a.Received
		case IsCode(err, "no_such_upload") && !recreated:
			recreated = true // expired (24 h) or gateway rebuilt
			ns, err := c.UploadCreate(ctx, size, sha, near)
			if err != nil {
				return "", err
			}
			st, offset = ns, 0
			if rs != nil {
				rs.SetResumableUpload(st.UploadID, sha, size)
			}
		case IsUnreachable(err):
			if failures >= len(c.RetryDelays) {
				return "", err
			}
			if err := sleep(ctx, c.RetryDelays[failures]); err != nil {
				return "", err
			}
			failures++
			if s, err := c.UploadStatus(ctx, st.UploadID); err == nil {
				offset = s.Received
			}
		default:
			return "", err
		}
	}
	return st.UploadID, nil
}

// ForgetResume drops every remembered session for this content.
func forget(rs ResumeStore, sha string, size int64) {
	if rs != nil {
		rs.SetResumableUpload("", sha, size)
	}
}

// UploadAndCommit uploads and commits; forgets the resumable session once committed (or
// once the gateway says it can never commit).
func (c *Client) UploadAndCommit(ctx context.Context, path string, req CommitRequest, rs ResumeStore,
	progress func(done, total int64)) (*CommitResponse, error) {
	sha, size, err := HashFile(path)
	if err != nil {
		return nil, err
	}
	near := req.ItemID
	if near == "" {
		near = req.ParentID
	}
	id, err := c.Upload(ctx, path, sha, size, near, rs, progress)
	if err != nil {
		return nil, err
	}
	r, err := c.Commit(ctx, id, req)
	if err == nil || IsCode(err, "file_hash_mismatch") || IsCode(err, "no_such_upload") {
		forget(rs, sha, size)
	}
	return r, err
}

func sleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// RangeResult is one Range read.
type RangeResult struct {
	Data  []byte
	Etag  string // content etag the bytes belong to ("c<N>")
	Total int64  // the file's size
	Start int64  // where Data begins (0 when the gateway sent the whole file)
}

func etagOf(h string) string { return strings.Trim(h, `"`) }

// parseTotal reads the size from "bytes a-b/total".
func parseTotal(cr string) (int64, bool) {
	i := strings.LastIndexByte(cr, '/')
	if i < 0 {
		return 0, false
	}
	n, err := strconv.ParseInt(cr[i+1:], 10, 64)
	return n, err == nil
}

// ReadRange fetches up to length bytes from offset. An empty file comes back as Total 0.
// Retries transport errors with the client's back-off.
func (c *Client) ReadRange(ctx context.Context, id string, offset, length int64) (*RangeResult, error) {
	hdr := map[string]string{"Range": fmt.Sprintf("bytes=%d-%d", offset, offset+max(length, 1)-1)}
	failures := 0
	for {
		resp, err := c.do(ctx, request{method: "GET", path: "v1/items/" + url.PathEscape(id) + "/content",
			headers: hdr, timeout: 5 * time.Minute})
		if err != nil {
			if IsUnreachable(err) && failures < len(c.RetryDelays) && failures < 2 {
				// Reads are interactive: two quick retries, not the upload back-off.
				if err := sleep(ctx, c.RetryDelays[failures]); err != nil {
					return nil, err
				}
				failures++
				continue
			}
			if a, ok := AsAPI(err); ok && a.Status == 416 {
				if offset == 0 && hdr != nil {
					hdr = nil // empty file: ask again without a Range
					continue
				}
				return nil, ErrChangedDuringTransfer // shorter on the NAS than we were told
			}
			return nil, err
		}
		r := &RangeResult{Data: resp.body, Etag: etagOf(resp.header.Get("ETag")), Start: offset}
		if t, ok := parseTotal(resp.header.Get("Content-Range")); resp.status == 206 && ok {
			r.Total = t
		} else {
			r.Start, r.Total = 0, int64(len(resp.body))
		}
		return r, nil
	}
}

// Download writes an item's whole content into f (truncated first) in Range slices, so a
// dropped connection resumes from the last byte written. Returns the content etag. If the
// NAS copy changes between slices it starts over (at most 3 times).
func (c *Client) Download(ctx context.Context, id string, f *os.File, progress func(done, total int64)) (string, error) {
	for restarts := 0; restarts < 3; restarts++ {
		tag, err := c.downloadOnce(ctx, id, f, progress)
		if !errors.Is(err, ErrChangedDuringTransfer) {
			return tag, err
		}
	}
	return "", ErrChangedDuringTransfer
}

func (c *Client) downloadOnce(ctx context.Context, id string, f *os.File, progress func(done, total int64)) (string, error) {
	if err := f.Truncate(0); err != nil {
		return "", err
	}
	var offset int64
	total := int64(-1)
	etag := ""
	for total < 0 || offset < total {
		r, err := c.ReadRange(ctx, id, offset, c.DownloadSlice)
		if err != nil {
			return "", err
		}
		if etag != "" && r.Etag != etag {
			return "", ErrChangedDuringTransfer
		}
		etag = r.Etag
		if r.Start == 0 && offset > 0 { // 200: the whole file; start the write over
			if err := f.Truncate(0); err != nil {
				return "", err
			}
			offset = 0
		}
		if _, err := f.WriteAt(r.Data, r.Start); err != nil {
			return "", err
		}
		total = r.Total
		offset = r.Start + int64(len(r.Data))
		if progress != nil {
			progress(offset, total)
		}
		if len(r.Data) == 0 {
			break
		}
	}
	return etag, f.Truncate(max(total, 0))
}
