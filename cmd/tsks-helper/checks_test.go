package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func checksumFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "payload/arm"), 0700); err != nil {
		t.Fatal(err)
	}
	var manifest strings.Builder
	for _, entry := range []struct{ name, data string }{{"version", "3.0.0\n"}, {".valid", "hnd\n"}, {"payload/arm/core", "fixture-core"}} {
		if err := os.WriteFile(filepath.Join(root, entry.name), []byte(entry.data), 0600); err != nil {
			t.Fatal(err)
		}
		hash := sha256.Sum256([]byte(entry.data))
		fmt.Fprintf(&manifest, "%x  %s\n", hash, entry.name)
	}
	if err := os.WriteFile(filepath.Join(root, "manifest.sha256"), []byte(manifest.String()), 0600); err != nil {
		t.Fatal(err)
	}
	return root
}

func TestCheckTreeCompletePackage(t *testing.T) {
	root := checksumFixture(t)
	if err := run([]string{"check-tree", root}); err != nil {
		t.Fatal(err)
	}
	// A final newline is conventional but not required by checksum manifests.
	name := filepath.Join(root, "manifest.sha256")
	data, _ := os.ReadFile(name)
	os.WriteFile(name, []byte(strings.TrimSuffix(string(data), "\n")), 0600)
	if err := checkTree(root); err != nil {
		t.Fatal(err)
	}
}

func TestCheckTreeRejectsIncompleteAndUnsafePackages(t *testing.T) {
	cases := map[string]func(string){
		"changed":  func(root string) { os.WriteFile(filepath.Join(root, "version"), []byte("tampered"), 0600) },
		"missing":  func(root string) { os.Remove(filepath.Join(root, "version")) },
		"unlisted": func(root string) { os.WriteFile(filepath.Join(root, "extra"), []byte("extra"), 0600) },
		"file symlink": func(root string) {
			os.Remove(filepath.Join(root, "version"))
			os.Symlink(".valid", filepath.Join(root, "version"))
		},
		"directory symlink": func(root string) { os.Symlink("payload", filepath.Join(root, "extra")) },
		"outside symlink":   func(root string) { os.Symlink(t.TempDir(), filepath.Join(root, "outside")) },
		"manifest symlink": func(root string) {
			os.Rename(filepath.Join(root, "manifest.sha256"), filepath.Join(root, "other"))
			os.Symlink("other", filepath.Join(root, "manifest.sha256"))
		},
		"fifo": func(root string) {
			if err := syscall.Mkfifo(filepath.Join(root, "fifo"), 0600); err != nil {
				t.Fatal(err)
			}
		},
		"directory in manifest": func(root string) {
			os.Remove(filepath.Join(root, "version"))
			os.Mkdir(filepath.Join(root, "version"), 0700)
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			root := checksumFixture(t)
			mutate(root)
			if err := checkTree(root); err == nil {
				t.Fatal("accepted invalid package")
			}
		})
	}
}

func TestCheckTreeManifestValidation(t *testing.T) {
	hash := strings.Repeat("a", 64)
	for _, manifest := range []string{
		"", "\n", hash + "  version\n\n", hash + " version\n" + hash + " version\n",
		hash + " ../version\n", hash + " /version\n", hash + " payload//arm/core\n",
		hash + " ./version\n", hash + " payload/../version\n", hash + " payload/./arm/core\n",
		hash + " version/\n", hash + " 版本\n", hash + " bad\\path\n", hash + " version extra\n",
		hash + " manifest.sha256\n", strings.Repeat("a", 63) + " version\n",
		strings.Repeat("A", 64) + " version\n", hash + " version\r\n", hash + "\u00a0version\n",
		strings.Repeat("x", maxPackageManifest+1),
	} {
		t.Run(fmt.Sprintf("%q", manifest[:min(len(manifest), 85)]), func(t *testing.T) {
			root := checksumFixture(t)
			os.WriteFile(filepath.Join(root, "manifest.sha256"), []byte(manifest), 0600)
			if err := checkTree(root); err == nil {
				t.Fatal("accepted invalid manifest")
			}
		})
	}
	root := checksumFixture(t)
	var manifest strings.Builder
	for i := 0; i <= maxPackageEntries; i++ {
		fmt.Fprintf(&manifest, "%s file-%d\n", hash, i)
	}
	os.WriteFile(filepath.Join(root, "manifest.sha256"), []byte(manifest.String()), 0600)
	if err := checkTree(root); err == nil || !strings.Contains(err.Error(), "too many") {
		t.Fatalf("entry limit: %v", err)
	}
	link := filepath.Join(t.TempDir(), "package")
	os.Symlink(root, link)
	if err := checkTree(link); err == nil {
		t.Fatal("accepted symlink package root")
	}
}

func TestSHA256RegularFileAndCorePointer(t *testing.T) {
	root := t.TempDir()
	os.Mkdir(filepath.Join(root, "core"), 0700)
	file := filepath.Join(root, "core/tailscale.combined")
	data := []byte(strings.Repeat("streamed core bytes\n", 200000))
	os.WriteFile(file, data, 0600)
	expected := sha256.Sum256(data)
	os.Symlink("core", filepath.Join(root, "current"))
	if hash, err := fileSHA256(filepath.Join(root, "current/tailscale.combined")); err != nil || hash != hex.EncodeToString(expected[:]) {
		t.Fatal(hash, err)
	}
	os.Symlink(file, filepath.Join(root, "leaf-link"))
	if err := syscall.Mkfifo(filepath.Join(root, "fifo"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"missing", "core", "leaf-link", "fifo"} {
		if _, err := fileSHA256(filepath.Join(root, name)); err == nil {
			t.Fatalf("accepted %s", name)
		}
	}
	// Shell callers compare a single bare digest, with no filename or JSON.
	os.WriteFile(file, []byte("abc"), 0600)
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	original := os.Stdout
	os.Stdout = writer
	err = run([]string{"sha256", file})
	writer.Close()
	os.Stdout = original
	output, readErr := io.ReadAll(reader)
	reader.Close()
	if err != nil || readErr != nil || string(output) != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n" {
		t.Fatal(string(output), err, readErr)
	}
}
