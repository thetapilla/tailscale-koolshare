// core_fixture_state creates a synthetic logged-in profile for offline tests.
// It uses random keys and an example.invalid login; it never contacts a server.
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/netip"
	"os"

	"tailscale.com/ipn"
	"tailscale.com/ipn/store"
	"tailscale.com/tailcfg"
	"tailscale.com/types/appctype"
	"tailscale.com/types/key"
	"tailscale.com/types/opt"
	"tailscale.com/types/persist"
)

func main() {
	if (len(os.Args) != 3 && len(os.Args) != 4) || (os.Args[2] != "true" && os.Args[2] != "false") || (len(os.Args) == 4 && os.Args[3] != "appc") {
		log.Fatal("usage: core_fixture_state STATE true|false [appc]")
	}
	st, err := store.NewFileStore(func(string, ...any) {}, os.Args[1])
	must(err)
	machine, err := key.NewMachine().MarshalText()
	must(err)
	must(st.WriteState(ipn.MachineKeyStateKey, machine))
	user := tailcfg.UserProfile{ID: 1001, LoginName: "offline@example.invalid", DisplayName: "Offline fixture"}
	p := ipn.NewPrefs()
	p.ControlURL = ipn.DefaultControlURL
	p.WantRunning = false
	p.CorpDNS = true
	p.Hostname = "core-smoke"
	p.AutoUpdate.Apply = opt.NewBool(os.Args[2] == "true")
	if len(os.Args) == 4 {
		p.AppConnector.Advertise = true
		p.AdvertiseRoutes = []netip.Prefix{netip.MustParsePrefix("192.0.2.0/24"), netip.MustParsePrefix("198.51.100.7/32"), netip.MustParsePrefix("2001:db8::7/128"), netip.MustParsePrefix("203.0.113.0/24")}
	}
	p.Persist = &persist.Persist{PrivateNodeKey: key.NewNode(), UserProfile: user, NodeID: "nTEST"}
	id, stateKey := ipn.ProfileID("a1b2"), ipn.StateKey("profile-a1b2")
	profile := ipn.LoginProfile{ID: id, Name: user.LoginName, Key: stateKey, UserProfile: user, NodeID: "nTEST", ControlURL: p.ControlURL}
	profiles, err := json.Marshal(map[ipn.ProfileID]ipn.LoginProfile{id: profile})
	must(err)
	must(st.WriteState(ipn.KnownProfilesStateKey, profiles))
	must(st.WriteState(stateKey, p.ToBytes()))
	must(st.WriteState(ipn.CurrentProfileStateKey, []byte(stateKey)))
	if len(os.Args) == 4 {
		info := appctype.RouteInfo{Domains: map[string][]netip.Addr{"app.example": {netip.MustParseAddr("198.51.100.7"), netip.MustParseAddr("2001:db8::7")}}, Control: []netip.Prefix{netip.MustParsePrefix("203.0.113.0/24")}}
		data, err := json.Marshal(info)
		must(err)
		must(st.WriteState(stateKey+"||_routeInfo", data))
	}

	must(os.Chmod(os.Args[1], 0600))
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
