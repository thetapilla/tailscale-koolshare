package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAtomicCorePointer(t *testing.T) {
	dir := t.TempDir()
	for _, v := range []string{"old", "new"} {
		os.MkdirAll(filepath.Join(dir, "cores", v), 0700)
	}
	p := filepath.Join(dir, "current")
	if e := atomicLink("cores/old", p); e != nil {
		t.Fatal(e)
	}
	if e := atomicLink("cores/new", p); e != nil {
		t.Fatal(e)
	}
	v, e := os.Readlink(p)
	if e != nil || v != "cores/new" {
		t.Fatal(v, e)
	}
	if e = atomicLink("../outside", p); e == nil {
		t.Fatal("unsafe link accepted")
	}
}
func TestLogRedactionAndBounds(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "daemon.log")
	input := strings.Repeat("safe line\n", 10000) + "https://login.tailscale.com/a/test-token tskey-auth-private token=private-value\n" + strings.Repeat("x", 100000) + "\nend\n"
	if e := logStream(strings.NewReader(input), p, 65536); e != nil {
		t.Fatal(e)
	}
	for _, file := range []string{p, p + ".1"} {
		b, e := os.ReadFile(file)
		if e != nil {
			t.Fatal(e)
		}
		if len(b) > 65536 || strings.Contains(string(b), "test-token") || strings.Contains(string(b), "auth-private") || strings.Contains(string(b), "private-value") {
			t.Fatal("unbounded or sensitive log")
		}
	}
	b, _ := os.ReadFile(p)
	if !strings.Contains(string(b), "end") || !strings.Contains(string(b), "oversized") {
		t.Fatal("reader did not recover after oversized line")
	}
}

func TestFirmwareFilePrimitives(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("PATH", dir)
	seen := map[string]bool{}
	for i := 0; i < 32; i++ {
		name, e := temporaryFile(filepath.Join(dir, ".status.XXXXXX"))
		if e != nil || seen[name] || filepath.Dir(name) != dir {
			t.Fatal(name, e)
		}
		seen[name] = true
		st, e := os.Lstat(name)
		if e != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 || st.Size() != 0 {
			t.Fatal(st, e)
		}
	}
	for _, template := range []string{filepath.Join(dir, "plain"), filepath.Join(dir, "missing", "xXXXXXX")} {
		if _, e := temporaryFile(template); e == nil {
			t.Fatal("invalid template accepted", template)
		}
	}
	fifo := filepath.Join(dir, "daemon.pipe")
	if e := run([]string{"fifo", fifo}); e != nil {
		t.Fatal(e)
	}
	st, e := os.Lstat(fifo)
	if e != nil || st.Mode()&os.ModeNamedPipe == 0 || st.Mode().Perm() != 0600 {
		t.Fatal(st, e)
	}
	if e := run([]string{"fifo", fifo}); e == nil {
		t.Fatal("existing FIFO replaced")
	}
	file := filepath.Join(dir, "keep")
	os.WriteFile(file, []byte("preserved"), 0600)
	link := filepath.Join(dir, "link")
	os.Symlink(file, link)
	for _, path := range []string{file, link} {
		if e := run([]string{"fifo", path}); e == nil {
			t.Fatal("existing path replaced", path)
		}
	}
	b, _ := os.ReadFile(file)
	if string(b) != "preserved" {
		t.Fatal("existing data modified")
	}
	for _, args := range [][]string{{"fifo"}, {"temp"}, {"temp", "invalid"}} {
		if e := run(args); e == nil {
			t.Fatal("invalid command accepted", args)
		}
	}
}
