package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

type statusResult struct {
	OK          bool     `json:"ok"`
	Version     string   `json:"version"`
	Backend     string   `json:"backend_state"`
	Online      *bool    `json:"online"`
	Codes       []string `json:"health_codes"`
	Messages    []string `json:"health_messages"`
	AuthURL     string   `json:"auth_url"`
	IPs         []string `json:"ips"`
	NodeID      string   `json:"node_id"`
	HaveNodeKey *bool    `json:"have_node_key"`
	WantRunning *bool    `json:"want_running"`
	LoggedOut   *bool    `json:"logged_out"`
	Sync        *bool    `json:"sync_enabled"`
	Monitoring  bool     `json:"monitoring_available"`
	Error       string   `json:"error,omitempty"`
}

func localClient(socket string) *http.Client {
	return &http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{Timeout: 2 * time.Second}).DialContext(ctx, "unix", socket)
	}}}
}
func localJSON(c *http.Client, path string, v any) error {
	r, e := c.Get("http://local-tailscaled.sock/localapi/v0/" + path)
	if e != nil {
		return e
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		return errors.New("LocalAPI request failed")
	}
	return json.NewDecoder(io.LimitReader(r.Body, 2*1024*1024)).Decode(v)
}

var authPattern = regexp.MustCompile(`https://login\.tailscale\.com/a/[^\s"<>]+`)

var keyPattern = regexp.MustCompile(`(?i)(tskey-[a-z0-9_-]+|(?:privkey|nodekey|machinekey):[a-f0-9]{32,}|(?:authkey|access_token|id_token|token|password)=[^\s&"<>]+)`)

func redact(s string) string {
	return keyPattern.ReplaceAllString(authPattern.ReplaceAllString(s, "[authorization link redacted]"), "[credential redacted]")
}
func readStatus(socket string) statusResult {
	out := statusResult{Codes: []string{}, Messages: []string{}, IPs: []string{}, Backend: "Unavailable"}
	c := localClient(socket)
	var s struct {
		Version      string
		BackendState string
		Health       []string
		AuthURL      string
		HaveNodeKey  *bool
		Self         *struct {
			ID           string
			Online       *bool
			TailscaleIPs []string
		}
	}
	if e := localJSON(c, "status?peers=false", &s); e != nil {
		out.Error = "LocalAPI unavailable"
		return out
	}
	out.OK = true
	out.Version = s.Version
	out.Backend = s.BackendState
	out.HaveNodeKey = s.HaveNodeKey
	if s.Self != nil {
		out.NodeID = s.Self.ID
		out.Online = s.Self.Online
		for _, s := range s.Self.TailscaleIPs {
			if net.ParseIP(s) != nil {
				out.IPs = append(out.IPs, s)
			}
		}
	}
	for _, s := range s.Health {
		out.Messages = append(out.Messages, redact(s))
	}
	if u, e := url.Parse(s.AuthURL); e == nil && u.Scheme == "https" && u.Hostname() == "login.tailscale.com" && u.User == nil && u.Port() == "" {
		out.AuthURL = s.AuthURL
	}
	var p struct {
		WantRunning *bool
		LoggedOut   *bool
		Sync        json.RawMessage
	}
	if e := localJSON(c, "prefs", &p); e == nil {
		out.WantRunning = p.WantRunning
		out.LoggedOut = p.LoggedOut
		yes := true
		out.Sync = &yes
		if len(p.Sync) > 0 && string(p.Sync) != "null" {
			var v bool
			if json.Unmarshal(p.Sync, &v) == nil {
				out.Sync = &v
			} else {
				out.Sync = nil
			}
		}
	}
	var h struct {
		Health *struct{ Warnings map[string]json.RawMessage }
	}
	if e := localJSON(c, "watch-ipn-bus?mask=130", &h); e == nil && h.Health != nil {
		for code := range h.Health.Warnings {
			if regexp.MustCompile(`^[a-z0-9-]{1,80}$`).MatchString(code) {
				out.Codes = append(out.Codes, code)
			}
		}
		out.Monitoring = out.WantRunning != nil && out.LoggedOut != nil && out.Sync != nil && out.Online != nil
	}
	return out
}
func ipv4CIDR(ip, mask string) (string, error) {
	a := net.ParseIP(ip).To4()
	m := net.ParseIP(mask).To4()
	if a == nil || m == nil {
		return "", errors.New("invalid IPv4 subnet")
	}
	bits, total := net.IPMask(m).Size()
	if total != 32 || bits == 0 {
		return "", errors.New("invalid IPv4 mask")
	}
	n := net.IPNet{IP: a.Mask(net.IPMask(m)), Mask: net.IPMask(m)}
	return strings.TrimSpace(n.String()), nil
}
