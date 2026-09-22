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
