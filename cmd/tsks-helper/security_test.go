package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixture(t *testing.T, name string, typ byte) (string, string, descriptor) {
	t.Helper()
	dir := t.TempDir()
	binary := make([]byte, 64)
	copy(binary, "\x7fELF")
	binary[4] = 1
	binary[5] = 1
	binary[18] = 40
	var archive bytes.Buffer
	gz := gzip.NewWriter(&archive)
	tr := tar.NewWriter(gz)
	h := &tar.Header{Name: name, Mode: 0755, Typeflag: typ, Size: int64(len(binary))}
	if typ == tar.TypeSymlink {
		h.Linkname = "/etc/passwd"
		h.Size = 0
	}
	if e := tr.WriteHeader(h); e != nil {
		t.Fatal(e)
	}
	if typ != tar.TypeSymlink {
		tr.Write(binary)
	}
	tr.Close()
	gz.Close()
	bh := sha256.Sum256(binary)
	ah := sha256.Sum256(archive.Bytes())
	d := descriptor{artifact{"https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v1.102.4-r1/tailscale-core_1.102.4_r1_arm.tar.gz", int64(archive.Len()), hex.EncodeToString(ah[:]), 64, hex.EncodeToString(bh[:])}, "1.102.4", "r1", "arm", strings.Repeat("a", 40), strings.Repeat("b", 64)}
	meta, _ := json.Marshal(d)
	os.WriteFile(filepath.Join(dir, "core.tgz"), archive.Bytes(), 0600)
	os.WriteFile(filepath.Join(dir, "meta.json"), meta, 0600)
	return filepath.Join(dir, "core.tgz"), filepath.Join(dir, "meta.json"), d
}
func TestExtractChecks(t *testing.T) {
	for _, tc := range []struct {
		name string
		typ  byte
		ok   bool
	}{{"tailscale.combined", tar.TypeReg, true}, {"../tailscale.combined", tar.TypeReg, false}, {"/tailscale.combined", tar.TypeReg, false}, {"tailscale.combined", tar.TypeSymlink, false}, {"install.sh", tar.TypeReg, false}} {
		t.Run(tc.name+string(tc.typ), func(t *testing.T) {
			src, meta, _ := fixture(t, tc.name, tc.typ)
			dest := filepath.Join(filepath.Dir(src), "out")
			e := extract(src, meta, dest)
			if (e == nil) != tc.ok {
				t.Fatalf("got %v", e)
			}
			if tc.ok {
				link, e := os.Readlink(filepath.Join(dest, "tailscale"))
				if e != nil || link != "tailscale.combined" {
					t.Fatal(link, e)
				}
			} else if _, e := os.Stat(dest); !os.IsNotExist(e) {
				t.Fatal("failed staging remained")
			}
		})
	}
}
func TestExtractHashAndArch(t *testing.T) {
	for _, field := range []string{"hash", "arch", "size"} {
		t.Run(field, func(t *testing.T) {
			src, meta, d := fixture(t, "tailscale.combined", tar.TypeReg)
			switch field {
			case "hash":
				d.SHA = strings.Repeat("0", 64)
			case "arch":
				d.Arch = "arm64"
				d.URL = strings.ReplaceAll(d.URL, "_arm.", "_arm64.")
			case "size":
				d.Unpacked = 65
			}
			b, _ := json.Marshal(d)
			os.WriteFile(meta, b, 0600)
			if e := extract(src, meta, filepath.Join(filepath.Dir(src), "out")); e == nil {
				t.Fatal("accepted invalid package")
			}
		})
	}
}
func TestExtractRefusesExistingDirectory(t *testing.T) {
	src, meta, _ := fixture(t, "tailscale.combined", tar.TypeReg)
	dest := t.TempDir()
	sentinel := filepath.Join(dest, "keep")
	os.WriteFile(sentinel, []byte("keep"), 0600)
	if e := extract(src, meta, dest); e == nil {
		t.Fatal("accepted existing directory")
	}
	if _, e := os.Stat(sentinel); e != nil {
		t.Fatal("modified existing directory")
	}
}
func TestSignedFeed(t *testing.T) {
	_, _, d := fixture(t, "tailscale.combined", tar.TypeReg)
	p := payload{1, "stable", d.Version, d.Build, d.Commit, d.Recipe, "2026-09-22T00:00:00Z", map[string]artifact{"arm": d.artifact, "arm64": d.artifact}}
	a := p.Artifacts["arm64"]
	a.URL = strings.ReplaceAll(a.URL, "_arm.", "_arm64.")
	p.Artifacts["arm64"] = a
	raw, _ := json.Marshal(p)
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	env := envelope{base64.StdEncoding.EncodeToString(raw), base64.StdEncoding.EncodeToString(ed25519.Sign(priv, raw))}
	dir := t.TempDir()
	key := filepath.Join(dir, "key")
	input := filepath.Join(dir, "feed")
	os.WriteFile(key, []byte(hex.EncodeToString(pub)), 0600)
	b, _ := json.Marshal(env)
	os.WriteFile(input, b, 0600)
	got, e := verifyFile(input, key, "arm")
	if e != nil || got.Version != p.Version {
		t.Fatal(got, e)
	}
	env.Payload = base64.StdEncoding.EncodeToString(append(raw, ' '))
	b, _ = json.Marshal(env)
	os.WriteFile(input, b, 0600)
	if _, e = verifyFile(input, key, "arm"); e == nil {
		t.Fatal("accepted changed payload")
	}
}
func TestURLsAndVersions(t *testing.T) {
	for _, s := range []string{"http://github.com/thetapilla/tailscale-koolshare/releases/download/x", "https://github.com.evil.test/thetapilla/tailscale-koolshare/releases/download/x", "https://github.com/other/repo/releases/download/x", "https://user@github.com/thetapilla/tailscale-koolshare/releases/download/x", "https://127.0.0.1/x"} {
		if trustedURL(s, false) {
			t.Fatal(s)
		}
	}
	for _, tc := range []struct {
		a, b string
		want int
	}{{"1.102.4", "1.94.2", 1}, {"1.102.4", "1.102.4", 0}, {"1.94.2", "1.102.4", -1}} {
		got, e := compareVersion(tc.a, tc.b)
		if e != nil || got != tc.want {
			t.Fatal(got, e)
		}
	}
	if _, e := compareVersion("1.2.3;id", "1.2.3"); e == nil {
		t.Fatal("invalid version")
	}
}
func TestDeadline(t *testing.T) {
	start := time.Now()
	e := deadlineCommand(100*time.Millisecond, []string{"/bin/sh", "-c", "sleep 10"}, nil, io.Discard, io.Discard)
	var code *exitError
	if !errors.As(e, &code) || code.code != 124 || time.Since(start) > 2*time.Second {
		t.Fatal(e)
	}
	e = deadlineCommand(time.Second, []string{"/bin/sh", "-c", "exit 7"}, nil, io.Discard, io.Discard)
	if !errors.As(e, &code) || code.code != 7 {
		t.Fatal(e)
	}
}
func TestCIDR(t *testing.T) {
	s, e := ipv4CIDR("192.168.50.1", "255.255.255.0")
	if e != nil || s != "192.168.50.0/24" {
		t.Fatal(s, e)
	}
	if _, e = ipv4CIDR("192.168.50.1", "255.0.255.0"); e == nil {
		t.Fatal("noncontiguous mask accepted")
	}
}
