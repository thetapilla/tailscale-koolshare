package main

import (
	"archive/tar"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const maxCore int64 = 12 * 1024 * 1024
const maxArchive int64 = 13 * 1024 * 1024

var versionRE = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)
var buildRE = regexp.MustCompile(`^r[1-9][0-9]{0,5}$`)
var hex40 = regexp.MustCompile(`^[a-f0-9]{40}$`)
var hex64 = regexp.MustCompile(`^[a-f0-9]{64}$`)

type artifact struct {
	URL       string `json:"url"`
	Size      int64  `json:"size"`
	SHA       string `json:"sha256"`
	Unpacked  int64  `json:"unpacked_size"`
	BinarySHA string `json:"binary_sha256"`
}
type payload struct {
	Schema    int                 `json:"schema"`
	Channel   string              `json:"channel"`
	Version   string              `json:"version"`
	Build     string              `json:"build"`
	Commit    string              `json:"source_commit"`
	Recipe    string              `json:"recipe_sha256"`
	Created   string              `json:"created_at"`
	Artifacts map[string]artifact `json:"artifacts"`
}
type envelope struct {
	Payload   string `json:"payload"`
	Signature string `json:"signature"`
}
type descriptor struct {
	artifact
	Version string `json:"version"`
	Build   string `json:"build"`
	Arch    string `json:"arch"`
	Commit  string `json:"source_commit"`
	Recipe  string `json:"recipe_sha256"`
}

func compareVersion(a, b string) (int, error) {
	if !versionRE.MatchString(a) || !versionRE.MatchString(b) {
		return 0, errors.New("invalid version")
	}
	aa := strings.Split(a, ".")
	bb := strings.Split(b, ".")
	for i := range aa {
		x, e := strconv.ParseUint(aa[i], 10, 32)
		if e != nil {
			return 0, e
		}
		y, e := strconv.ParseUint(bb[i], 10, 32)
		if e != nil {
			return 0, e
		}
		if x < y {
			return -1, nil
		}
		if x > y {
			return 1, nil
		}
	}
	return 0, nil
}
func readLimited(path string, max int64) ([]byte, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, max+1))
	if int64(len(b)) > max {
		return nil, errors.New("file too large")
	}
	return b, e
}
func readKey(path string, size int) ([]byte, error) {
	b, e := readLimited(path, 512)
	if e != nil {
		return nil, e
	}
	k, e := hex.DecodeString(strings.TrimSpace(string(b)))
	if e != nil || len(k) != size {
		return nil, errors.New("invalid signing key")
	}
	return k, nil
}
func keygen(private, public string) error {
	pub, priv, e := ed25519.GenerateKey(rand.Reader)
	if e != nil {
		return e
	}
	if e = writeExclusive(private, []byte(hex.EncodeToString(priv)+"\n"), 0600); e != nil {
		return e
	}
	return writeExclusive(public, []byte(hex.EncodeToString(pub)+"\n"), 0644)
}
func writeExclusive(path string, b []byte, mode os.FileMode) error {
	f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if e != nil {
		return e
	}
	defer f.Close()
	if _, e = f.Write(b); e != nil {
		return e
	}
	return f.Sync()
}
func signFile(input, key, output string) error {
	b, e := readLimited(input, 32768)
	if e != nil {
		return e
	}
	var p payload
	if e = json.Unmarshal(b, &p); e != nil {
		return e
	}
	if e = validPayload(p); e != nil {
		return e
	}
	k, e := readKey(key, ed25519.PrivateKeySize)
	if e != nil {
		return e
	}
	v := envelope{base64.StdEncoding.EncodeToString(b), base64.StdEncoding.EncodeToString(ed25519.Sign(k, b))}
	out, e := json.Marshal(v)
	if e != nil {
		return e
	}
	return os.WriteFile(output, append(out, '\n'), 0644)
}
func validPayload(p payload) error {
	if p.Schema != 1 || p.Channel != "stable" || !versionRE.MatchString(p.Version) || !buildRE.MatchString(p.Build) || !hex40.MatchString(p.Commit) || !hex64.MatchString(p.Recipe) {
		return errors.New("invalid feed metadata")
	}
	if _, e := time.Parse(time.RFC3339, p.Created); e != nil {
		return errors.New("invalid feed date")
	}
	if len(p.Artifacts) != 2 {
		return errors.New("both architectures required")
	}
	for _, arch := range []string{"arm", "arm64"} {
		a, ok := p.Artifacts[arch]
		if !ok {
			return errors.New("architecture missing")
		}
		d := descriptor{a, p.Version, p.Build, arch, p.Commit, p.Recipe}
		if e := validDescriptor(d); e != nil {
			return e
		}
	}
	return nil
}
func validDescriptor(d descriptor) error {
	if !versionRE.MatchString(d.Version) || !buildRE.MatchString(d.Build) || !hex40.MatchString(d.Commit) || !hex64.MatchString(d.Recipe) || (d.Arch != "arm" && d.Arch != "arm64") {
		return errors.New("invalid core metadata")
	}
	if d.Size <= 0 || d.Size > maxArchive || d.Unpacked <= 0 || d.Unpacked > maxCore || !hex64.MatchString(d.SHA) || !hex64.MatchString(d.BinarySHA) {
		return errors.New("invalid core size or hash")
	}
	want := fmt.Sprintf("https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v%s-%s/tailscale-core_%s_%s_%s.tar.gz", d.Version, d.Build, d.Version, d.Build, d.Arch)
	if d.URL != want {
		return errors.New("untrusted core URL")
	}
	return nil
}
func verifyFile(input, key, arch string) (descriptor, error) {
	var zero descriptor
	b, e := readLimited(input, 65536)
	if e != nil {
		return zero, e
	}
	var v envelope
	if e = json.Unmarshal(b, &v); e != nil {
		return zero, e
	}
	raw, e := base64.StdEncoding.DecodeString(v.Payload)
	if e != nil || len(raw) > 32768 {
		return zero, errors.New("invalid payload")
	}
	sig, e := base64.StdEncoding.DecodeString(v.Signature)
	if e != nil {
		return zero, e
	}
	pub, e := readKey(key, ed25519.PublicKeySize)
	if e != nil {
		return zero, e
	}
	if !ed25519.Verify(pub, raw, sig) {
		return zero, errors.New("signature verification failed")
	}
	var p payload
	if e = json.Unmarshal(raw, &p); e != nil {
		return zero, e
	}
	if e = validPayload(p); e != nil {
		return zero, e
	}
	a, ok := p.Artifacts[arch]
	if !ok {
		return zero, errors.New("unsupported architecture")
	}
	return descriptor{a, p.Version, p.Build, arch, p.Commit, p.Recipe}, nil
}
func trustedURL(s string, redirect bool) bool {
	u, e := url.Parse(s)
	if e != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" || u.Fragment != "" {
		return false
	}
	switch u.Hostname() {
	case "github.com":
		return strings.HasPrefix(u.Path, "/thetapilla/tailscale-koolshare/releases/download/")
	case "release-assets.githubusercontent.com", "objects.githubusercontent.com":
		return redirect
	}
	return false
}
func fetch(src, dest string, limit int64) error {
	if !trustedURL(src, false) {
		return errors.New("download URL not allowed")
	}
	c := &http.Client{Timeout: 180 * time.Second, CheckRedirect: func(r *http.Request, v []*http.Request) error {
		if len(v) > 5 || !trustedURL(r.URL.String(), true) {
			return errors.New("redirect not allowed")
		}
		return nil
	}}
	r, e := c.Get(src)
	if e != nil {
		return e
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		return fmt.Errorf("download HTTP %d", r.StatusCode)
	}
	if r.ContentLength > limit {
		return errors.New("download too large")
	}
	f, e := os.CreateTemp(filepath.Dir(dest), ".download-*")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	n, e := io.Copy(f, io.LimitReader(r.Body, limit+1))
	if e == nil && n > limit {
		e = errors.New("download limit exceeded")
	}
	if e == nil {
		e = f.Sync()
	}
	closeErr := f.Close()
	if e != nil {
		return e
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(name, dest)
}
func extract(src, meta, dest string) error {
	b, e := readLimited(meta, 65536)
	if e != nil {
		return e
	}
	var d descriptor
	if e = json.Unmarshal(b, &d); e != nil {
		return e
	}
	if e = validDescriptor(d); e != nil {
		return e
	}
	f, e := os.Open(src)
	if e != nil {
		return e
	}
	defer f.Close()
	st, e := f.Stat()
	if e != nil {
		return e
	}
	if st.Size() != d.Size {
		return errors.New("archive size mismatch")
	}
	sum := sha256.New()
	if _, e = io.Copy(sum, io.LimitReader(f, maxArchive+1)); e != nil {
		return e
	}
	if hex.EncodeToString(sum.Sum(nil)) != d.SHA {
		return errors.New("archive hash mismatch")
	}
	if _, e = f.Seek(0, 0); e != nil {
		return e
	}
	if _, e = os.Lstat(dest); !os.IsNotExist(e) {
		return errors.New("extraction destination must not exist")
	}
	if e = os.Mkdir(dest, 0700); e != nil {
		return e
	}
	ok := false
	defer func() {
		if !ok {
			_ = os.RemoveAll(dest)
		}
	}()
	gz, e := gzip.NewReader(f)
	if e != nil {
		return e
	}
	defer gz.Close()
	tr := tar.NewReader(io.LimitReader(gz, maxCore+65536))
	h, e := tr.Next()
	if e != nil {
		return e
	}
	if h.Name != "tailscale.combined" || (h.Typeflag != tar.TypeReg && h.Typeflag != tar.TypeRegA) || h.Size != d.Unpacked || len(h.PAXRecords) != 0 {
		return errors.New("unexpected core archive member")
	}
	out, e := os.OpenFile(filepath.Join(dest, "tailscale.combined"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0700)
	if e != nil {
		return e
	}
	sum = sha256.New()
	_, e = io.Copy(io.MultiWriter(out, sum), tr)
	if e == nil {
		e = out.Sync()
	}
	out.Close()
	if e != nil {
		return e
	}
	if hex.EncodeToString(sum.Sum(nil)) != d.BinarySHA {
		return errors.New("binary hash mismatch")
	}
	if _, e = tr.Next(); e != io.EOF {
		return errors.New("extra archive member")
	}
	if e = checkELF(filepath.Join(dest, "tailscale.combined"), d.Arch); e != nil {
		return e
	}
	if e = os.Chmod(filepath.Join(dest, "tailscale.combined"), 0755); e != nil {
		return e
	}
	for _, name := range []string{"tailscale", "tailscaled"} {
		if e = os.Symlink("tailscale.combined", filepath.Join(dest, name)); e != nil {
			return e
		}
	}
	if e = os.WriteFile(filepath.Join(dest, "descriptor.json"), b, 0644); e != nil {
		return e
	}
	ok = true
	return nil
}

// checkELF validates a core without executing it or invoking firmware utilities.
// The installer and the update archive reader use the same architecture checks.
func checkELF(path, arch string) error {
	var wantClass byte
	var wantMachine uint16
	switch arch {
	case "arm":
		wantClass, wantMachine = 1, 40
	case "arm64":
		wantClass, wantMachine = 2, 183
	default:
		return errors.New("unsupported ELF architecture")
	}
	st, e := os.Lstat(path)
	if e != nil {
		return e
	}
	if !st.Mode().IsRegular() {
		return errors.New("ELF must be a regular file")
	}
	f, e := os.Open(path)
	if e != nil {
		return e
	}
	defer f.Close()
	var head [20]byte
	if _, e = io.ReadFull(f, head[:]); e != nil {
		return fmt.Errorf("read ELF header: %w", e)
	}
	if string(head[:4]) != "\x7fELF" || head[4] != wantClass || head[5] != 1 || binary.LittleEndian.Uint16(head[18:20]) != wantMachine {
		return errors.New("ELF architecture mismatch")
	}
	return nil
}
