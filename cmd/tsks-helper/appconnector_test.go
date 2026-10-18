package main

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

func prefixes(list string) []netip.Prefix {
	out := []netip.Prefix{}
	if list != "" {
		for _, s := range strings.Split(list, ",") {
			out = append(out, netip.MustParsePrefix(s))
		}
	}
	return out
}
func TestPlanRoutes(t *testing.T) {
	for _, tc := range []struct {
		name, p, d, l   string
		connector, keep bool
		want            string
	}{
		{"restart", "0.0.0.0/0,::/0,198.51.100.7/32", "0.0.0.0/0,::/0,198.51.100.7/32", "198.51.100.7/32", true, true, "0.0.0.0/0,::/0,198.51.100.7/32"},
		{"connector-owns-removed-custom", "0.0.0.0/0,::/0,198.51.100.7/32", "0.0.0.0/0,::/0", "198.51.100.7/32", true, true, "0.0.0.0/0,::/0,198.51.100.7/32"},
		{"new-lan", "198.51.100.7/32", "192.168.1.0/24", "198.51.100.7/32", true, false, "192.168.1.0/24,198.51.100.7/32"},
		{"disabled", "198.51.100.7/32,192.168.1.0/24", "192.168.1.0/24", "198.51.100.7/32", false, false, "192.168.1.0/24"},
		{"covered-and-prohibited-not-advertised", "192.168.1.0/24", "192.168.1.0/24", "192.168.1.2/32,127.0.0.1/32,::1/128", true, true, "192.168.1.0/24"},
		{"set-comparison", "2001:db8::1/128,192.0.2.0/24", "192.0.2.1/24,192.0.2.0/24", "2001:db8::1/128,2001:db8::1/128", true, true, "192.0.2.0/24,2001:db8::1/128"},
		{"one-exit-default", "0.0.0.0/0", "0.0.0.0/0,::/0", "", true, false, "0.0.0.0/0,::/0"},
		{"control-normalized", "203.0.113.1/24", "", "203.0.113.7/24", true, true, "203.0.113.0/24"},
		{"stable-sort", "2001:db8::2/128,198.51.100.3/32,198.51.100.2/32", "192.0.2.0/24", "2001:db8::2/128,198.51.100.3/32,198.51.100.2/32", true, false, "192.0.2.0/24,198.51.100.2/32,198.51.100.3/32,2001:db8::2/128"},
		{"empty", "", "", "", false, true, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			keep, list := planRoutes(prefixes(tc.p), prefixes(tc.d), prefixes(tc.l), tc.connector)
			if keep != tc.keep || !reflect.DeepEqual(list, prefixes(tc.want)) {
				t.Fatalf("got %v %v", keep, list)
			}
		})
	}
}
func TestRoutesPlanLocalAPI(t *testing.T) {
	for _, tc := range []struct {
		name, prefs, info, list string
		exit, connector         bool
		code                    int
		want                    string
		fail                    bool
	}{
		{"keeps", "{\"AdvertiseRoutes\":[\"198.51.100.1/32\"]}", `{"Domains":{"a.test":["198.51.100.1"]}}`, "", false, true, 200, "keep", false},
		{"control", "{\"AdvertiseRoutes\":[\"203.0.113.0/24\"]}", `{"Control":["203.0.113.0/24"]}`, "192.0.2.0/24", false, true, 200, "set\t192.0.2.0/24,203.0.113.0/24", false},
		{"control-default-owned-by-exit", `{"AdvertiseRoutes":["0.0.0.0/0","::/0"]}`, `{"Control":["0.0.0.0/0","::/0"]}`, "", false, true, 200, "set\t", false},
		{"empty-prefix-invalid", `{"AdvertiseRoutes":[""]}`, `{}`, "", false, true, 200, "", true},
		{"disabled-skips-info", `{"AdvertiseRoutes":["198.51.100.1/32"]}`, ``, "", false, false, 501, "set\t", false},
		{"null-empty", `{"AdvertiseRoutes":null}`, `null`, "", false, true, 200, "keep", false},
		{"exit", `{"AdvertiseRoutes":["0.0.0.0/0"]}`, `{}`, "", true, false, 200, "set\t", false},
		{"unsupported", `{"AdvertiseRoutes":[]}`, ``, "", false, true, 501, "", true},
		{"missing-routes", `{}`, `{}`, "", false, true, 200, "", true},
		{"bad-prefix", `{"AdvertiseRoutes":["oops"]}`, `{}`, "", false, true, 200, "", true},
		{"bad-info", `{"AdvertiseRoutes":[]}`, `{"Domains":{"x":["oops"]}}`, "", false, true, 200, "", true},
		{"bad-list", `{"AdvertiseRoutes":[]}`, `{}`, "x", false, true, 200, "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/localapi/v0/prefs":
					fmt.Fprint(w, tc.prefs)
				case "/localapi/v0/appc-route-info":
					if !tc.connector {
						t.Error("read info while disabled")
					}
					w.WriteHeader(tc.code)
					fmt.Fprint(w, tc.info)
				default:
					t.Error(r.URL.Path)
				}
			})
			got, e := routesPlan(c, tc.list, tc.exit, tc.connector)
			if (e != nil) != tc.fail || got != tc.want {
				t.Fatalf("got %q, %v", got, e)
			}
		})
	}
	if _, err := routesPlan(localClient("/missing-tsks-appc-socket"), "", false, true); err == nil {
		t.Fatal("missing socket accepted")
	}
	for _, args := range [][]string{{"routes-plan"}, {"routes-plan", "x", "", "true", "1"}, {"routes-plan", "x", "", "0", ""}, {"appc-prime"}, {"appc-prime", "x", "extra"}} {
		if run(args) == nil {
			t.Fatalf("accepted %v", args)
		}
	}
}
func appSelf() *connectorSelf {
	return &connectorSelf{Tags: []string{"tag:connector"}, TailscaleIPs: []string{"100.64.0.10", "fd7a:115c:a1e0::a"}, PeerAPIURL: []string{"http://[fd7a:115c:a1e0::a]:4444", "http://100.64.0.10:4444"}, CapMap: map[string][]json.RawMessage{"tailscale.com/app-connectors": {
		json.RawMessage(`{"connectors":["tag:connector"],"domains":["A.Example.","*.example.org"]}`), json.RawMessage(`{"connectors":["*"],"domains":["a.example","b.example"]}`), json.RawMessage(`{"connectors":["tag:other"],"domains":["other.example"]}`),
	}}}
}
func TestAssignedDomainsAndOwnPeerAPI(t *testing.T) {
	self := appSelf()
	domains, wildcards, e := assignedDomains(self)
	if e != nil || wildcards != 1 || !reflect.DeepEqual(domains, []string{"*.example.org", "a.example", "b.example"}) {
		t.Fatalf("%v %v %v", domains, wildcards, e)
	}
	u, ip, e := ownPeerAPI(self)
	if e != nil || u.String() != "http://100.64.0.10:4444/dns-query" || ip.String() != "100.64.0.10" {
		t.Fatalf("%v %v %v", u, ip, e)
	}
	for _, bad := range []string{"https://100.64.0.10:4444", "http://100.64.0.11:4444", "http://example.org:4444", "http://user:pass@100.64.0.10:4444", "http://100.64.0.10:4444/x", "http://100.64.0.10:4444/?x=y", "http://100.64.0.10:4444/#x", "http://100.64.0.10", "http://100.64.0.10:0", "http://100.64.0.10:99999", "http://[fd7a:115c:a1e0::a%25eth0]:4444"} {
		t.Run(bad, func(t *testing.T) {
			s := appSelf()
			s.PeerAPIURL = []string{bad}
			if _, _, e := ownPeerAPI(s); e == nil {
				t.Fatal("unsafe URL accepted")
			}
		})
	}
	self = appSelf()
	self.PeerAPIURL = self.PeerAPIURL[:1]
	if _, ip, e := ownPeerAPI(self); e != nil || !ip.Is6() {
		t.Fatal("IPv6 fallback failed")
	}
	for _, bad := range []json.RawMessage{json.RawMessage(`{"domains":1}`), json.RawMessage(`{"connectors":["*"],"domains":["bad name"]}`), json.RawMessage(`{"connectors":["*"],"domains":["*.*.example"]}`)} {
		self := appSelf()
		self.CapMap["tailscale.com/app-connectors"] = []json.RawMessage{bad}
		if _, _, err := assignedDomains(self); err == nil {
			t.Fatalf("bad config accepted: %s", bad)
		}
	}
}
func TestConnectorStatus(t *testing.T) {
	for _, tc := range []struct {
		name    string
		prefs   *connectorPrefs
		prefsOK bool
		code    int
		fail    bool
	}{
		{"enabled", &connectorPrefs{boolPtr(true)}, true, 200, false}, {"disabled", &connectorPrefs{boolPtr(false)}, true, 501, false}, {"missing", nil, true, 200, true}, {"missing-advertise", &connectorPrefs{}, true, 200, true}, {"prefs-failed", &connectorPrefs{boolPtr(true)}, false, 200, true}, {"unsupported", &connectorPrefs{boolPtr(true)}, true, 501, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			c2 := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.WriteHeader(tc.code)
				fmt.Fprint(w, `{"Domains":{"A.Example.":["198.51.100.1","198.51.100.1"],"a.example":["2001:db8::1"]}}`)
			})
			out := readConnectorStatus(c2, appSelf(), tc.prefs, tc.prefsOK)
			if (out.Error != "") != tc.fail {
				t.Fatalf("%+v", out)
			}
			expected := 0
			if tc.prefsOK && tc.prefs != nil && tc.prefs.Advertise != nil && *tc.prefs.Advertise {
				expected = 1
			}
			if calls != expected {
				t.Fatalf("extra read: %d", calls)
			}
			if tc.name == "enabled" && len(out.Learned["a.example"]) != 2 {
				t.Fatalf("%+v", out)
			}
			if tc.name == "prefs-failed" && out.Advertise != nil {
				t.Fatal("unknown preference became false")
			}
		})
	}
}
func boolPtr(v bool) *bool { return &v }

func TestAppcPrime(t *testing.T) {
	for _, mode := range []string{"learn-after-poll", "denied", "timeout", "bad-dns", "redirect", "no-domains", "wildcards-only", "api-failure", "poll-failure"} {
		t.Run(mode, func(t *testing.T) {
			var mu sync.Mutex
			queries := map[string]map[uint16]bool{}
			polls := 0
			peer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/dns-query" || r.Method != "GET" || r.Header.Get("Accept") != "application/dns-message" {
					t.Errorf("bad DoH request %v", r)
				}
				raw, e := base64.RawURLEncoding.Strict().DecodeString(r.URL.Query().Get("dns"))
				if e != nil || len(raw) < 17 {
					t.Error("bad base64 DNS")
					w.WriteHeader(400)
					return
				}
				if binary.BigEndian.Uint16(raw[:2]) != 0 || raw[2] != 1 || raw[5] != 1 || binary.BigEndian.Uint16(raw[len(raw)-2:]) != 1 {
					t.Errorf("bad header %x", raw)
				}
				labels := []string{}
				offset := 12
				for offset < len(raw) && raw[offset] != 0 {
					n := int(raw[offset])
					offset++
					if offset+n > len(raw) {
						t.Error("invalid DNS label")
						return
					}
					labels = append(labels, string(raw[offset:offset+n]))
					offset += n
				}
				if offset+5 != len(raw) {
					t.Errorf("unexpected DNS sections %x", raw)
					return
				}
				domain := strings.Join(labels, ".")
				typ := binary.BigEndian.Uint16(raw[offset+1 : offset+3])
				if typ != 1 && typ != 28 {
					t.Errorf("type %d", typ)
				}
				mu.Lock()
				if queries[domain] == nil {
					queries[domain] = map[uint16]bool{}
				}
				queries[domain][typ] = true
				mu.Unlock()
				switch mode {
				case "denied":
					w.WriteHeader(403)
				case "timeout":
					<-r.Context().Done()
				case "bad-dns":
					fmt.Fprint(w, "not DNS")
				case "redirect":
					w.Header().Set("Location", "http://elsewhere.invalid/")
					w.WriteHeader(302)
				default:
					raw[2] = 0x81
					w.Header().Set("Content-Type", "application/dns-message")
					w.Write(raw)
				}
			}))
			defer peer.Close()
			self := appSelf()
			u, _ := url.Parse(peer.URL)
			self.PeerAPIURL = []string{peer.URL}
			self.TailscaleIPs = []string{u.Hostname()}
			if mode == "no-domains" {
				self.CapMap = nil
			}
			if mode == "wildcards-only" {
				self.CapMap["tailscale.com/app-connectors"] = []json.RawMessage{json.RawMessage(`{"connectors":["*"],"domains":["*.example.org"]}`)}
			}
			local := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/localapi/v0/status":
					if mode == "api-failure" {
						w.WriteHeader(500)
						return
					}
					json.NewEncoder(w).Encode(map[string]any{"Self": self})
				case "/localapi/v0/appc-route-info":
					polls++
					if mode == "poll-failure" {
						w.WriteHeader(500)
						return
					}
					if mode == "learn-after-poll" && polls >= 2 {
						fmt.Fprint(w, `{"Domains":{"a.example":["198.51.100.1"],"b.example":["198.51.100.1","2001:db8::1"]}}`)
					} else {
						fmt.Fprint(w, `{}`)
					}
				default:
					t.Error(r.URL.Path)
				}
			})
			client := peer.Client()
			client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
			start := time.Now()
			out, e := appcPrime(local, client, primeTiming{500 * time.Millisecond, 50 * time.Millisecond, 100 * time.Millisecond, 5 * time.Millisecond, 4})
			if time.Since(start) > 600*time.Millisecond {
				t.Fatal("prime exceeded total budget")
			}
			if mode == "api-failure" || mode == "poll-failure" {
				if e == nil {
					t.Fatal("LocalAPI failure ignored")
				}
				return
			}
			if e != nil {
				t.Fatal(e)
			}
			if mode == "no-domains" || mode == "wildcards-only" {
				if len(queries) != 0 || polls != 0 {
					t.Fatal("queried unassigned domain")
				}
				return
			}
			mu.Lock()
			defer mu.Unlock()
			for _, d := range []string{"a.example", "b.example"} {
				if len(queries[d]) != 2 {
					t.Fatalf("A/AAAA missing: %v", queries)
				}
			}
			if len(queries) != 2 {
				t.Fatalf("queried wildcard/unassigned: %v", queries)
			}
			if out.Assigned != 3 || out.Wildcards != 1 || out.Denied != (mode == "denied") {
				t.Fatalf("%+v", out)
			}
			if mode == "learn-after-poll" {
				if out.Primed != 2 || out.Learned != 2 || polls < 2 {
					t.Fatalf("%+v polls %d", out, polls)
				}
			} else if out.Primed != 0 {
				t.Fatalf("false prime success: %+v", out)
			}
		})
	}
}
func TestPrimeBoundedLargeAssignment(t *testing.T) {
	peer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
	defer peer.Close()
	self := appSelf()
	u, _ := url.Parse(peer.URL)
	self.PeerAPIURL = []string{peer.URL}
	self.TailscaleIPs = []string{u.Hostname()}
	domains := []string{}
	for i := 0; i < 500; i++ {
		domains = append(domains, fmt.Sprintf("host%d.example", i))
	}
	attr, _ := json.Marshal(map[string]any{"connectors": []string{"*"}, "domains": domains})
	self.CapMap["tailscale.com/app-connectors"] = []json.RawMessage{attr}
	local := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "status") {
			json.NewEncoder(w).Encode(map[string]any{"Self": self})
		} else {
			fmt.Fprint(w, `{}`)
		}
	})
	start := time.Now()
	out, e := appcPrime(local, peer.Client(), primeTiming{500 * time.Millisecond, 30 * time.Millisecond, 50 * time.Millisecond, time.Millisecond, 4})
	if e != nil || out.Assigned != 500 || out.Primed != 0 || time.Since(start) > 600*time.Millisecond {
		t.Fatalf("%+v %v elapsed %v", out, e, time.Since(start))
	}
}

func TestFullStatusConnector(t *testing.T) {
	for _, prefs := range []string{`{"AppConnector":{"Advertise":true}}`, `{"AppConnector":{"Advertise":false}}`, `{}`, `{"AppConnector":{"Advertise":"true"}}`} {
		t.Run(prefs, func(t *testing.T) {
			calls := 0
			socket := filepath.Join(t.TempDir(), "s")
			ln, err := net.Listen("unix", socket)
			if err != nil {
				t.Fatal(err)
			}
			srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/localapi/v0/status":
					json.NewEncoder(w).Encode(map[string]any{"BackendState": "Running", "Self": appSelf()})
				case "/localapi/v0/prefs":
					fmt.Fprint(w, prefs)
				case "/localapi/v0/appc-route-info":
					calls++
					fmt.Fprint(w, `{"Domains":{"a.example":["198.51.100.7"]}}`)
				case "/localapi/v0/watch-ipn-bus":
					fmt.Fprint(w, `{"Health":{"Warnings":{}}}`)
				default:
					t.Error(r.URL.Path)
				}
			})}
			go srv.Serve(ln)
			defer srv.Close()
			result := readStatus(socket)
			if !result.OK || !reflect.DeepEqual(result.AppConnector.Tags, []string{"tag:connector"}) || len(result.AppConnector.Assigned) != 3 || len(result.IPs) != 2 {
				t.Fatalf("%+v", result)
			}
			switch prefs {
			case `{"AppConnector":{"Advertise":true}}`:
				if calls != 1 || result.AppConnector.Advertise == nil || !*result.AppConnector.Advertise || len(result.AppConnector.Learned) != 1 || result.AppConnector.Error != "" {
					t.Fatalf("%+v, calls %d", result.AppConnector, calls)
				}
			case `{"AppConnector":{"Advertise":false}}`:
				if calls != 0 || result.AppConnector.Advertise == nil || *result.AppConnector.Advertise || result.AppConnector.Error != "" {
					t.Fatalf("%+v, calls %d", result.AppConnector, calls)
				}
			default:
				if calls != 0 || result.AppConnector.Advertise != nil || result.AppConnector.Error == "" {
					t.Fatalf("unknown prefs became false: %+v", result.AppConnector)
				}
			}
		})
	}
}

func TestPrimeProductionTransportUsesOwnAddress(t *testing.T) {
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("ALL_PROXY", "http://127.0.0.1:1")
	count := 0
	var mu sync.Mutex
	peer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host, _, e := net.SplitHostPort(r.RemoteAddr)
		if e != nil || host != "127.0.0.1" {
			t.Errorf("unexpected source %s", r.RemoteAddr)
		}
		query, e := base64.RawURLEncoding.DecodeString(r.URL.Query().Get("dns"))
		if e != nil {
			t.Error(e)
			return
		}
		query[2] |= 0x80
		mu.Lock()
		count++
		mu.Unlock()
		w.Write(query)
	}))
	defer peer.Close()
	self := appSelf()
	self.PeerAPIURL = []string{peer.URL}
	self.TailscaleIPs = []string{"127.0.0.1"}
	self.CapMap["tailscale.com/app-connectors"] = []json.RawMessage{json.RawMessage(`{"connectors":["*"],"domains":["a.example"]}`)}
	local := connectFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "status") {
			json.NewEncoder(w).Encode(map[string]any{"Self": self})
		} else {
			fmt.Fprint(w, `{"Domains":{"a.example":["198.51.100.1"]}}`)
		}
	})
	out, e := appcPrime(local, nil, primeTiming{time.Second, 200 * time.Millisecond, 200 * time.Millisecond, time.Millisecond, 2})
	if e != nil || out.Primed != 1 || count != 2 {
		t.Fatalf("%+v %v queries %d", out, e, count)
	}
}
