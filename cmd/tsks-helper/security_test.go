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
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
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
func TestMirrorURLs(t *testing.T) {
	const feed = releasePrefix + "core-stable/manifest.json"
	const core = releasePrefix + "core-v1.104.6-r2/tailscale-core_1.104.6_r2_arm64.tar.gz"
	for src, want := range map[string]string{
		feed: "https://tscore.thetapilla.xyz/core-stable/manifest.json",
		core: "https://tscore.thetapilla.xyz/core-v1.104.6-r2/tailscale-core_1.104.6_r2_arm64.tar.gz",
		// The mirror carries the signed feed and core archives only.
		releasePrefix + "v3.4.0/SHA256SUMS":                                         "",
		releasePrefix + "core-stable/../core-stable/manifest.json":                  "",
		releasePrefix + "core-stable/manifest.json?x=1":                             "",
		releasePrefix + "core-stable/manifest%2Ejson":                               "",
		"https://github.com/other/repo/releases/download/core-stable/manifest.json": "",
	} {
		if got := mirrorURL(src); got != want {
			t.Fatalf("mirrorURL(%q) = %q, want %q", src, got, want)
		}
	}
	mirror := mirrorURL(core)
	if !trustedURL(mirror, false) {
		t.Fatal("mirror download rejected")
	}
	// Nothing may redirect to the mirror, and the mirror may not redirect at all.
	if trustedURL(mirror, true) {
		t.Fatal("mirror accepted as a redirect target")
	}
	if noRedirect(nil, nil) == nil {
		t.Fatal("mirror redirect accepted")
	}
	for _, s := range []string{
		"http://tscore.thetapilla.xyz/core-stable/manifest.json",
		"https://tscore.thetapilla.xyz:8443/core-stable/manifest.json",
		"https://user@tscore.thetapilla.xyz/core-stable/manifest.json",
		"https://tscore.thetapilla.xyz.evil.test/core-stable/manifest.json",
		"https://evil.thetapilla.xyz/core-stable/manifest.json",
		"https://tscore.thetapilla.xyz/connectivity-test/8m.bin",
		"https://tscore.thetapilla.xyz/core-stable/manifest.json#x",
	} {
		if trustedURL(s, false) {
			t.Fatal(s)
		}
	}
}

func TestFetchFallsBackToTheNextSource(t *testing.T) {
	body := bytes.Repeat([]byte("core"), 4096)
	var mirrorHits atomic.Int32
	serve := func(h http.HandlerFunc) string {
		s := httptest.NewServer(h)
		t.Cleanup(s.Close)
		return s.URL
	}
	good := func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != "tsks-helper" {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		mirrorHits.Add(1)
		w.Write(body)
	}
	mirror := serve(good)
	release := map[string]http.HandlerFunc{
		"http error": func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) },
		"too large":  func(w http.ResponseWriter, r *http.Request) { w.Write(append(body, body...)) },
		"cut short": func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Length", "16384")
			w.Write(body[:100])
		},
		"stalled": func(w http.ResponseWriter, r *http.Request) {
			w.Write(body[:100])
			w.(http.Flusher).Flush()
			select {
			case <-r.Context().Done():
			case <-time.After(5 * time.Second):
			}
		},
		"redirect": func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, mirror, http.StatusFound) },
	}
	for name, handler := range release {
		t.Run(name, func(t *testing.T) {
			dest := filepath.Join(t.TempDir(), "file")
			before := mirrorHits.Load()
			start := time.Now()
			used, e := fetchFrom([]source{{serve(handler), noRedirect, 3 * time.Second}, {mirror, noRedirect, 3 * time.Second}}, dest, int64(len(body)), 300*time.Millisecond)
			if e != nil || used != 1 || mirrorHits.Load() != before+1 {
				t.Fatal(used, e)
			}
			if got, _ := os.ReadFile(dest); !bytes.Equal(got, body) {
				t.Fatal("fallback stored a different file")
			}
			if time.Since(start) > 2*time.Second {
				t.Fatal("a failed source held up the fallback")
			}
			if left, _ := filepath.Glob(filepath.Join(filepath.Dir(dest), ".download-*")); len(left) != 0 {
				t.Fatal(left)
			}
		})
	}
	// A working first source is final: the mirror is never contacted.
	dest := filepath.Join(t.TempDir(), "file")
	before := mirrorHits.Load()
	unused := serve(func(w http.ResponseWriter, r *http.Request) { t.Error("mirror contacted") })
	if used, e := fetchFrom([]source{{mirror, noRedirect, 3 * time.Second}, {unused, noRedirect, 3 * time.Second}}, dest, int64(len(body)), time.Second); e != nil || used != 0 || mirrorHits.Load() != before+1 {
		t.Fatal(used, e)
	}
	// When every source fails nothing is stored and both reasons are reported.
	dest = filepath.Join(t.TempDir(), "file")
	down := serve(release["http error"])
	_, e := fetchFrom([]source{{down, noRedirect, 3 * time.Second}, {serve(release["stalled"]), noRedirect, 3 * time.Second}}, dest, int64(len(body)), 300*time.Millisecond)
	if e == nil || !strings.Contains(e.Error(), "HTTP 502") || !strings.Contains(e.Error(), "stalled") {
		t.Fatal(e)
	}
	if _, statErr := os.Stat(dest); !os.IsNotExist(statErr) {
		t.Fatal("a failed download left a file")
	}
	// The total limit ends a transfer that keeps trickling.
	slow := serve(func(w http.ResponseWriter, r *http.Request) {
		for i := 0; i < 100; i++ {
			w.Write(body[:10])
			w.(http.Flusher).Flush()
			time.Sleep(50 * time.Millisecond)
		}
	})
	if _, e = fetchFrom([]source{{slow, noRedirect, 400 * time.Millisecond}}, dest, int64(len(body)), time.Second); e == nil || !strings.Contains(e.Error(), "timed out") {
		t.Fatal(e)
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

func TestELFCommand(t *testing.T) {
	// The check must work with no firmware tools available in PATH.
	t.Setenv("PATH", t.TempDir())
	for _, arch := range []string{"arm", "arm64"} {
		t.Run(arch, func(t *testing.T) {
			head := make([]byte, 64)
			copy(head, "\x7fELF")
			head[4], head[5], head[18] = 1, 1, 40
			other := "arm64"
			if arch == "arm64" {
				head[4], head[18], other = 2, 183, "arm"
			}
			file := filepath.Join(t.TempDir(), "core")
			if e := os.WriteFile(file, head, 0600); e != nil {
				t.Fatal(e)
			}
			if e := run([]string{"elf", file, arch}); e != nil {
				t.Fatal(e)
			}
			for _, invalid := range []string{other, "amd64", ""} {
				if e := run([]string{"elf", file, invalid}); e == nil {
					t.Fatalf("accepted architecture %q", invalid)
				}
			}
			for _, offset := range []int{0, 4, 5, 18, 19} {
				broken := append([]byte(nil), head...)
				broken[offset] ^= 0xff
				os.WriteFile(file, broken, 0600)
				if e := checkELF(file, arch); e == nil {
					t.Fatalf("accepted corrupt header at offset %d", offset)
				}
			}
			os.WriteFile(file, head[:19], 0600)
			if e := checkELF(file, arch); e == nil {
				t.Fatal("accepted truncated header")
			}
			os.WriteFile(file, head, 0600)
			link := file + ".link"
			os.Symlink(file, link)
			for _, path := range []string{link, file + ".missing", filepath.Dir(file)} {
				if e := checkELF(path, arch); e == nil {
					t.Fatalf("accepted non-regular file %s", path)
				}
			}
		})
	}
	if e := run([]string{"elf"}); e == nil {
		t.Fatal("accepted missing arguments")
	}
}
