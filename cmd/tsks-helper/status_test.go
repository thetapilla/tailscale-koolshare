package main

import (
	"encoding/json"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

func TestSanitizedLocalAPI(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "s")
	ln, e := net.Listen("unix", socket)
	if e != nil {
		t.Fatal(e)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/localapi/v0/status", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"Version":"1.104.1-t9a522a978","BackendState":"Running","Self":{"Online":false,"TailscaleIPs":["100.1.2.3"]},"Health":["warning"],"AuthURL":"javascript:alert(1)","PrivateKey":"never-export-me"}`))
	})
	mux.HandleFunc("/localapi/v0/prefs", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"WantRunning":true,"LoggedOut":false,"Sync":null,"Persist":{"PrivateNodeKey":"never-export-me"}}`))
	})
	mux.HandleFunc("/localapi/v0/watch-ipn-bus", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("mask") != "130" {
			t.Error("wrong health mask")
		}
		w.Write([]byte(`{"Health":{"Warnings":{"not-in-map-poll":{"Text":"test"}}}}`))
	})
	srv := &http.Server{Handler: mux}
	go srv.Serve(ln)
	defer srv.Close()
	s := readStatus(socket)
	if !s.OK || !s.Monitoring || s.Online == nil || *s.Online || s.AuthURL != "" || len(s.Codes) != 1 {
		t.Fatalf("%+v", s)
	}
	if s.Version != "1.104.1" || s.VersionLong != "1.104.1-t9a522a978" {
		t.Fatalf("version contract: %+v", s)
	}
	b, _ := json.Marshal(s)
	if strings.Contains(string(b), "never-export") {
		t.Fatal("secret leaked")
	}
}

func TestReleaseVersion(t *testing.T) {
	for _, tt := range []struct{ long, short string }{
		{"1.104.1", "1.104.1"},
		{"1.104.1-t9a522a978", "1.104.1"},
		{"1.104.1-t9a522a978-g123456abc", "1.104.1"},
		{"1.104.10-t9a522a978", "1.104.10"},
		{"1.104.1-12-t9a522a978", "1.104.1-12-t9a522a978"},
		{"1.104.1-dev20261008-t9a522a978", "1.104.1-dev20261008-t9a522a978"},
		{"1.104.1-t9a522a978-dirty", "1.104.1-t9a522a978-dirty"},
		{"1.104.1-rc1", "1.104.1-rc1"},
		{"1.104.1evil", "1.104.1evil"},
		{"1.104.1-t9a522a97", "1.104.1-t9a522a97"},
		{"1.104.1-t9a522a9786", "1.104.1-t9a522a9786"},
		{"1.104.1-t9a522a97z", "1.104.1-t9a522a97z"},
		{"1.104.1-t9a522a978\n", "1.104.1-t9a522a978\n"},
		{"", ""},
	} {
		t.Run(tt.long, func(t *testing.T) {
			if got := releaseVersion(tt.long); got != tt.short {
				t.Fatalf("releaseVersion(%q) = %q, want %q", tt.long, got, tt.short)
			}
		})
	}
}
func TestUnavailableSocket(t *testing.T) {
	s := readStatus(filepath.Join(t.TempDir(), "missing"))
	if s.OK || s.Monitoring || s.Online != nil || s.Backend != "Unavailable" {
		t.Fatalf("%+v", s)
	}
}
