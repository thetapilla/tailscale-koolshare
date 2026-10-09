package main

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

func connectFixture(t *testing.T, handler http.HandlerFunc) *http.Client {
	t.Helper()
	socket := filepath.Join(t.TempDir(), "s")
	ln, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: handler}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close() })
	c := localClient(socket)
	t.Cleanup(c.CloseIdleConnections)
	return c
}

func TestConnectPreservesPreferences(t *testing.T) {
	for _, tc := range []struct {
		want         string
		state        string
		patch, login bool
	}{
		{"false", "NeedsLogin", true, true}, {"null", "NeedsLogin", true, true},
		{"true", "NeedsLogin", false, true}, {"false", "Starting", true, false},
		{"true", "Running", false, false}, {"true", "Starting", false, false},
		{"true", "Stopped", false, false}, {"true", "NeedsMachineAuth", false, false},
	} {
		t.Run(tc.want+"/"+tc.state, func(t *testing.T) {
			var calls []string
			c := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
				calls = append(calls, r.Method+" "+r.URL.RequestURI())
				body, _ := io.ReadAll(r.Body)
				switch calls[len(calls)-1] {
				case "GET /localapi/v0/prefs":
					fmt.Fprintf(w, `{"WantRunning":%s,"CorpDNS":false}`, tc.want)
				case "PATCH /localapi/v0/prefs":
					if string(body) != `{"WantRunning":true,"WantRunningSet":true}` {
						t.Errorf("unexpected preference edit: %s", body)
					}
					if r.Header.Get("Content-Type") != "application/json" {
						t.Error("missing JSON content type")
					}
					w.WriteHeader(200)
				case "GET /localapi/v0/status?peers=false":
					fmt.Fprintf(w, `{"BackendState":%q}`, tc.state)
				case "POST /localapi/v0/login-interactive":
					if len(body) != 0 {
						t.Errorf("unexpected login body: %s", body)
					}
					w.WriteHeader(204)
				default:
					t.Errorf("unexpected request: %s", calls[len(calls)-1])
					w.WriteHeader(404)
				}
			})
			result, err := connectLocal(c)
			if err != nil {
				t.Fatal(err)
			}
			if !result.OK || result.WantRunningSet != tc.patch || result.LoginRequested != tc.login || result.BackendState != tc.state {
				t.Fatalf("unexpected result: %+v", result)
			}
			want := []string{"GET /localapi/v0/prefs"}
			if tc.patch {
				want = append(want, "PATCH /localapi/v0/prefs")
			}
			want = append(want, "GET /localapi/v0/status?peers=false")
			if tc.login {
				want = append(want, "POST /localapi/v0/login-interactive")
			}
			if !reflect.DeepEqual(calls, want) {
				t.Fatalf("requests = %v, want %v", calls, want)
			}
		})
	}
}

func TestConnectStopsAfterAnyFailedRequest(t *testing.T) {
	for _, stage := range []int{0, 1, 2, 3} {
		for _, failure := range []string{"status", "redirect", "timeout"} {
			t.Run(fmt.Sprintf("%d/%s", stage, failure), func(t *testing.T) {
				var mu sync.Mutex
				count := 0
				c := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
					mu.Lock()
					index := count
					count++
					mu.Unlock()
					if index == stage {
						switch failure {
						case "status":
							w.WriteHeader(500)
						case "redirect":
							w.Header().Set("Location", "/unexpected")
							w.WriteHeader(307)
						case "timeout":
							<-r.Context().Done()
						}
						return
					}
					switch index {
					case 0:
						fmt.Fprint(w, `{"WantRunning":false}`)
					case 1:
						w.WriteHeader(200)
					case 2:
						fmt.Fprint(w, `{"BackendState":"NeedsLogin"}`)
					default:
						w.WriteHeader(204)
					}
				})
				c.Timeout = 100 * time.Millisecond
				result, err := connectLocal(c)
				if err == nil || result.OK {
					t.Fatalf("unexpected success: %+v", result)
				}
				mu.Lock()
				got := count
				mu.Unlock()
				if got != stage+1 {
					t.Fatalf("sent %d requests, expected %d", got, stage+1)
				}
			})
		}
	}
}

func TestConnectRejectsInvalidReadsAndUnexpectedPatchSuccess(t *testing.T) {
	for _, tc := range []struct {
		prefs, state string
		patch        int
	}{
		{`broken`, `{"BackendState":"Running"}`, 200},
		{`{"WantRunning":true}`, `broken`, 200},
		{`{"WantRunning":true}`, `{}`, 200},
		{`{"WantRunning":false}`, `{"BackendState":"Running"}`, 204},
	} {
		c := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
			if r.Method == "PATCH" {
				w.WriteHeader(tc.patch)
				return
			}
			if strings.HasSuffix(r.URL.Path, "prefs") {
				fmt.Fprint(w, tc.prefs)
			} else {
				fmt.Fprint(w, tc.state)
			}
		})
		if _, err := connectLocal(c); err == nil {
			t.Fatalf("accepted invalid response %+v", tc)
		}
	}
	if _, err := connectLocal(localClient(filepath.Join(t.TempDir(), "missing"))); err == nil {
		t.Fatal("accepted missing socket")
	}
	if err := run([]string{"connect"}); err == nil {
		t.Fatal("accepted missing socket argument")
	}
	if err := run([]string{"connect", "/x", "/prefs"}); err == nil {
		t.Fatal("accepted arbitrary path argument")
	}
}
