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
		w.Write([]byte(`{"Version":"1.94.2","BackendState":"Running","Self":{"Online":false,"TailscaleIPs":["100.1.2.3"]},"Health":["warning"],"AuthURL":"javascript:alert(1)","PrivateKey":"never-export-me"}`))
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
	b, _ := json.Marshal(s)
	if strings.Contains(string(b), "never-export") {
		t.Fatal("secret leaked")
	}
}
func TestUnavailableSocket(t *testing.T) {
	s := readStatus(filepath.Join(t.TempDir(), "missing"))
	if s.OK || s.Monitoring || s.Online != nil || s.Backend != "Unavailable" {
		t.Fatalf("%+v", s)
	}
}
