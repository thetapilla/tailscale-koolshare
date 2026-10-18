package main

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// These wire fields match Tailscale 1.104.1's ipnstate.PeerStatus,
// ipn.AppConnectorPrefs and appctype.RouteInfo. No identity fields are retained.
type connectorSelf struct {
	Tags         []string
	CapMap       map[string][]json.RawMessage
	PeerAPIURL   []string
	TailscaleIPs []string
}
type connectorPrefs struct{ Advertise *bool }
type connectorRouteInfo struct {
	Control   []netip.Prefix
	Domains   map[string][]netip.Addr
	Wildcards []string
}
type connectorStatus struct {
	Advertise *bool               `json:"advertise"`
	Tags      []string            `json:"tags"`
	Assigned  []string            `json:"assigned_domains"`
	Wildcards int                 `json:"wildcards"`
	Learned   map[string][]string `json:"learned"`
	Error     string              `json:"error"`
}

func emptyConnectorStatus() connectorStatus {
	return connectorStatus{Tags: []string{}, Assigned: []string{}, Learned: map[string][]string{}}
}

// Only known fixed reasons cross the shell/log boundary; server error bodies,
// paths, and internal settings are never included.
func connectorJSON(ctx context.Context, c *http.Client, path string, v any) error {
	req, err := http.NewRequestWithContext(ctx, "GET", "http://local-tailscaled.sock/localapi/v0/"+path, nil)
	if err != nil {
		return errors.New("LocalAPI request invalid")
	}
	resp, err := c.Do(req)
	if err != nil {
		return errors.New("LocalAPI unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotImplemented {
		return errors.New("app connector unsupported")
	}
	if resp.StatusCode != http.StatusOK {
		return errors.New("LocalAPI request failed")
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 2*1024*1024+1))
	if err != nil || len(b) > 2*1024*1024 || json.Unmarshal(b, v) != nil {
		return errors.New("LocalAPI response invalid")
	}
	return nil
}
func normalizedPrefixes(list []netip.Prefix) []netip.Prefix {
	out := []netip.Prefix{}
	seen := map[netip.Prefix]bool{}
	for _, p := range list {
		if p.IsValid() {
			p = p.Masked()
			if !seen[p] {
				seen[p] = true
				out = append(out, p)
			}
		}
	}
	return out
}

// The union is exact, not a subnet-cover operation: learned addresses that
// never entered current preferences must not cause another route write.
func planRoutes(p, d, l []netip.Prefix, connector bool) (keep bool, list []netip.Prefix) {
	current := map[netip.Prefix]bool{}
	for _, v := range normalizedPrefixes(p) {
		current[v] = true
	}
	target := map[netip.Prefix]bool{}
	list = normalizedPrefixes(d)
	for _, v := range list {
		target[v] = true
	}
	if connector {
		retained := []netip.Prefix{}
		for _, v := range normalizedPrefixes(l) {
			if current[v] && !target[v] {
				target[v] = true
				retained = append(retained, v)
			}
		}
		sort.Slice(retained, func(i, j int) bool {
			if n := retained[i].Addr().Compare(retained[j].Addr()); n != 0 {
				return n < 0
			}
			return retained[i].Bits() < retained[j].Bits()
		})
		list = append(list, retained...)
	}
	keep = len(current) == len(target)
	for v := range current {
		if !target[v] {
			keep = false
		}
	}
	return keep, list
}
func routeInfoPrefixes(info connectorRouteInfo) []netip.Prefix {
	// The CLI encodes default prefixes exclusively through --advertise-exit-node.
	// Retaining a Control default while EXIT is false would make its actual
	// mutation differ from this plan, so exit defaults belong only to D.
	out := []netip.Prefix{}
	for _, p := range info.Control {
		if p.Bits() != 0 {
			out = append(out, p)
		}
	}
	for _, addrs := range info.Domains {
		for _, ip := range addrs {
			if ip.IsValid() {
				ip = ip.Unmap()
				out = append(out, netip.PrefixFrom(ip, ip.BitLen()))
			}
		}
	}
	return out
}
func routesPlan(c *http.Client, list string, exit, connector bool) (string, error) {
	d := []netip.Prefix{}
	if list != "" {
		for _, raw := range strings.Split(list, ",") {
			p, e := netip.ParsePrefix(raw)
			if e != nil || p.Bits() == 0 {
				return "", errors.New("invalid managed routes")
			}
			d = append(d, p.Masked())
		}
	}
	if exit {
		d = append(d, netip.MustParsePrefix("0.0.0.0/0"), netip.MustParsePrefix("::/0"))
	}
	var prefs struct{ AdvertiseRoutes json.RawMessage }
	if err := connectorJSON(context.Background(), c, "prefs", &prefs); err != nil {
		return "", err
	}
	if len(prefs.AdvertiseRoutes) == 0 {
		return "", errors.New("current routes unavailable")
	}
	var p []netip.Prefix
	if err := json.Unmarshal(prefs.AdvertiseRoutes, &p); err != nil {
		return "", errors.New("current routes invalid")
	}
	for _, prefix := range p {
		if !prefix.IsValid() {
			return "", errors.New("current routes invalid")
		}
	}
	var info connectorRouteInfo
	if connector {
		if err := connectorJSON(context.Background(), c, "appc-route-info", &info); err != nil {
			return "", err
		}
	}
	keep, desired := planRoutes(p, d, routeInfoPrefixes(info), connector)
	if keep {
		return "keep", nil
	}
	values := []string{}
	for _, p := range desired {
		if p.Bits() != 0 {
			values = append(values, p.String())
		}
	}
	return "set\t" + strings.Join(values, ","), nil
}
func canonicalDomain(s string) (string, bool) {
	s = strings.ToLower(strings.TrimSuffix(s, "."))
	if len(s) == 0 || len(s) > 253 {
		return "", false
	}
	for _, label := range strings.Split(s, ".") {
		if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return "", false
		}
		for _, r := range label {
			if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
				return "", false
			}
		}
	}
	return s, true
}
func assignedDomains(self *connectorSelf) ([]string, int, error) {
	out := []string{}
	if self == nil {
		return out, 0, nil
	}
	tags := map[string]bool{"*": true}
	for _, t := range self.Tags {
		tags[t] = true
	}
	seen := map[string]bool{}
	wildcards := 0
	for _, raw := range self.CapMap["tailscale.com/app-connectors"] {
		var attr struct {
			Connectors []string
			Domains    []string
		}
		if json.Unmarshal(raw, &attr) != nil {
			return nil, 0, errors.New("app connector configuration invalid")
		}
		matches := false
		for _, tag := range attr.Connectors {
			matches = matches || tags[tag]
		}
		if !matches {
			continue
		}
		for _, domain := range attr.Domains {
			wildcard := strings.HasPrefix(domain, "*.")
			name := domain
			if wildcard {
				name = name[2:]
			}
			name, ok := canonicalDomain(name)
			if !ok {
				return nil, 0, errors.New("app connector domain invalid")
			}
			if wildcard {
				name = "*." + name
			}
			if !seen[name] {
				seen[name] = true
				out = append(out, name)
				if wildcard {
					wildcards++
				}
			}
		}
	}
	sort.Strings(out)
	return out, wildcards, nil
}
func learnedDomains(info connectorRouteInfo) map[string][]string {
	out := map[string][]string{}
	for domain, ips := range info.Domains {
		name, ok := canonicalDomain(domain)
		if !ok {
			continue
		}
		seen := map[string]bool{}
		for _, v := range out[name] {
			seen[v] = true
		}
		if out[name] == nil {
			out[name] = []string{}
		}
		for _, ip := range ips {
			if ip.IsValid() && ip.Zone() == "" {
				v := ip.Unmap().String()
				if !seen[v] {
					out[name] = append(out[name], v)
					seen[v] = true
				}
			}
		}
		sort.Strings(out[name])
	}
	return out
}
func readConnectorStatus(c *http.Client, self *connectorSelf, prefs *connectorPrefs, prefsOK bool) connectorStatus {
	out := emptyConnectorStatus()
	if self != nil {
		out.Tags = append(out.Tags, self.Tags...)
	}
	domains, wildcards, err := assignedDomains(self)
	if err != nil {
		out.Error = err.Error()
	} else {
		out.Assigned = domains
		out.Wildcards = wildcards
	}
	if !prefsOK || prefs == nil || prefs.Advertise == nil {
		out.Error = "app connector preference unavailable"
		return out
	}
	out.Advertise = prefs.Advertise
	if *out.Advertise {
		var info connectorRouteInfo
		if err := connectorJSON(context.Background(), c, "appc-route-info", &info); err != nil {
			out.Error = err.Error()
		} else {
			out.Learned = learnedDomains(info)
		}
	}
	return out
}
func ownPeerAPI(self *connectorSelf) (*url.URL, netip.Addr, error) {
	if self == nil {
		return nil, netip.Addr{}, errors.New("LocalAPI self unavailable")
	}
	own := map[netip.Addr]bool{}
	for _, raw := range self.TailscaleIPs {
		if ip, e := netip.ParseAddr(raw); e == nil && ip.Zone() == "" {
			own[ip.Unmap()] = true
		}
	}
	var best *url.URL
	var bestIP netip.Addr
	for _, raw := range self.PeerAPIURL {
		u, e := url.Parse(raw)
		if e != nil || u.Scheme != "http" || u.User != nil || u.Opaque != "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" || u.RawPath != "" {
			continue
		}
		ip, e := netip.ParseAddr(u.Hostname())
		if e != nil || ip.Zone() != "" || !own[ip.Unmap()] {
			continue
		}
		port, e := strconv.Atoi(u.Port())
		if e != nil || port < 1 || port > 65535 {
			continue
		}
		if best == nil || bestIP.Is6() && ip.Is4() {
			best = u
			bestIP = ip
		}
	}
	if best == nil {
		return nil, netip.Addr{}, errors.New("own peerAPI unavailable")
	}
	best.Path = "/dns-query"
	return best, bestIP, nil
}

// RFC 8484 recommends ID zero to improve HTTP cache efficiency. The recursive
// query contains exactly one IN question and no credentials or extra records.
func dnsQuestion(domain string, typ uint16) []byte {
	b := make([]byte, 12)
	b[2] = 1
	b[5] = 1
	for _, label := range strings.Split(domain, ".") {
		b = append(b, byte(len(label)))
		b = append(b, label...)
	}
	b = append(b, 0, byte(typ>>8), byte(typ), 0, 1)
	return b
}
func queryPeerDNS(ctx context.Context, c *http.Client, base *url.URL, domain string, typ uint16) (success, denied bool) {
	query := dnsQuestion(domain, typ)
	u := *base
	u.RawQuery = "dns=" + base64.RawURLEncoding.EncodeToString(query)
	req, err := http.NewRequestWithContext(ctx, "GET", u.String(), nil)
	if err != nil {
		return false, false
	}
	req.Header.Set("Accept", "application/dns-message")
	resp, err := c.Do(req)
	if err != nil {
		return false, false
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusForbidden {
		return false, true
	}
	if resp.StatusCode != http.StatusOK {
		return false, false
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 65536))
	// Tailscale relays wire DNS with the original question intact. Learning is
	// confirmed through LocalAPI, never inferred from an HTTP success alone.
	if err != nil || len(b) < len(query) || len(b) > 65535 || binary.BigEndian.Uint16(b[:2]) != 0 || b[2]&0xF8 != 0x80 || b[3]&0x0F != 0 || binary.BigEndian.Uint16(b[4:6]) != 1 || string(b[12:len(query)]) != string(query[12:]) {
		return false, false
	}
	return true, false
}

type primeResult struct {
	Assigned  int  `json:"assigned"`
	Primed    int  `json:"primed"`
	Learned   int  `json:"learned"`
	Wildcards int  `json:"wildcards"`
	Denied    bool `json:"denied"`
}
type primeTiming struct {
	total, queries, poll, interval time.Duration
	workers                        int
}

var defaultPrimeTiming = primeTiming{18 * time.Second, 5 * time.Second, 10 * time.Second, 250 * time.Millisecond, 8}

func appcPrime(c *http.Client, peerClient *http.Client, timing primeTiming) (primeResult, error) {
	out := primeResult{}
	ctx, cancel := context.WithTimeout(context.Background(), timing.total)
	defer cancel()
	var status struct{ Self *connectorSelf }
	if err := connectorJSON(ctx, c, "status?peers=false", &status); err != nil {
		return out, err
	}
	if status.Self == nil {
		return out, errors.New("LocalAPI self unavailable")
	}
	domains, wildcards, err := assignedDomains(status.Self)
	if err != nil {
		return out, err
	}
	out.Assigned = len(domains)
	out.Wildcards = wildcards
	if len(domains) == wildcards {
		return out, nil
	}
	base, ownIP, err := ownPeerAPI(status.Self)
	if err != nil {
		return out, err
	}
	if peerClient == nil {
		transport := &http.Transport{DialContext: (&net.Dialer{Timeout: 5 * time.Second, LocalAddr: &net.TCPAddr{IP: net.IP(ownIP.AsSlice())}}).DialContext, MaxConnsPerHost: timing.workers}
		defer transport.CloseIdleConnections()
		peerClient = &http.Client{Timeout: 5 * time.Second, Transport: transport, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	}
	// A shared query budget and fixed worker pool keep large app assignments
	// bounded below the shell's 20-second deadline. Unqueried domains remain
	// visibly unprimed; later client lookups can populate them normally.
	queryCtx, queryCancel := context.WithTimeout(ctx, timing.queries)
	type job struct {
		domain string
		typ    uint16
	}
	jobs := make(chan job)
	queried := map[string]bool{}
	var mu sync.Mutex
	var wg sync.WaitGroup
	for i := 0; i < timing.workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for task := range jobs {
				if queryCtx.Err() != nil {
					continue
				}
				ok, denied := queryPeerDNS(queryCtx, peerClient, base, task.domain, task.typ)
				mu.Lock()
				queried[task.domain] = queried[task.domain] || ok
				out.Denied = out.Denied || denied
				mu.Unlock()
			}
		}()
	}
send:
	for _, domain := range domains {
		if strings.HasPrefix(domain, "*.") {
			continue
		}
		for _, typ := range []uint16{1, 28} {
			select {
			case jobs <- job{domain, typ}:
			case <-queryCtx.Done():
				break send
			}
		}
	}
	close(jobs)
	wg.Wait()
	queryCancel()
	pollCtx, pollCancel := context.WithTimeout(ctx, timing.poll)
	defer pollCancel()
	var latest connectorRouteInfo
	for {
		var info connectorRouteInfo
		if err := connectorJSON(pollCtx, c, "appc-route-info", &info); err != nil {
			if pollCtx.Err() != nil {
				return out, nil
			}
			return out, err
		}
		latest = info
		learned := learnedDomains(latest)
		unique := map[string]bool{}
		out.Primed = 0
		for _, ips := range learned {
			for _, ip := range ips {
				unique[ip] = true
			}
		}
		out.Learned = len(unique)
		for domain, ok := range queried {
			if ok && len(learned[domain]) > 0 {
				out.Primed++
			}
		}
		if out.Primed == len(domains)-wildcards || out.Denied || len(queried) == 0 {
			return out, nil
		}
		select {
		case <-pollCtx.Done():
			return out, nil
		case <-time.After(timing.interval):
		}
	}
}
func runRoutesPlan(args []string) error {
	if len(args) != 5 {
		return errors.New("routes-plan SOCKET LIST EXIT CONNECTOR")
	}
	if (args[3] != "0" && args[3] != "1") || (args[4] != "0" && args[4] != "1") {
		return errors.New("invalid route options")
	}
	result, err := routesPlan(localClient(args[1]), args[2], args[3] == "1", args[4] == "1")
	if err != nil {
		return err
	}
	fmt.Println(result)
	return nil
}
