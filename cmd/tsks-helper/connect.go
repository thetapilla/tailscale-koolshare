package main

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

type connectResult struct {
	OK             bool   `json:"ok"`
	WantRunningSet bool   `json:"want_running_set"`
	LoginRequested bool   `json:"login_requested"`
	BackendState   string `json:"backend_state"`
}

// connectLocal changes only connection intent. The CLI's first-login startup
// path can replace preferences, including settings applied before login.
// Keep the request paths and the masked preference body fixed in this helper.
func connectLocal(c *http.Client) (connectResult, error) {
	var out connectResult
	var prefs struct{ WantRunning *bool }
	if err := localJSON(c, "prefs", &prefs); err != nil {
		return out, errors.New("connect: cannot read LocalAPI preferences")
	}
	if prefs.WantRunning == nil || !*prefs.WantRunning {
		const body = `{"WantRunning":true,"WantRunningSet":true}`
		req, _ := http.NewRequest(http.MethodPatch, "http://local-tailscaled.sock/localapi/v0/prefs", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if err := connectWrite(c, req, true); err != nil {
			return out, err
		}
		out.WantRunningSet = true
	}
	var status struct{ BackendState string }
	if err := localJSON(c, "status?peers=false", &status); err != nil || status.BackendState == "" {
		return out, errors.New("connect: cannot read LocalAPI state")
	}
	out.BackendState = status.BackendState
	if status.BackendState == "NeedsLogin" {
		req, _ := http.NewRequest(http.MethodPost, "http://local-tailscaled.sock/localapi/v0/login-interactive", nil)
		if err := connectWrite(c, req, false); err != nil {
			return out, err
		}
		out.LoginRequested = true
	}
	out.OK = true
	return out, nil
}

func connectWrite(c *http.Client, req *http.Request, exactOK bool) error {
	r, err := c.Do(req)
	if err != nil {
		return fmt.Errorf("connect: LocalAPI %s request failed", req.Method)
	}
	defer r.Body.Close()
	if (exactOK && r.StatusCode != http.StatusOK) || (!exactOK && (r.StatusCode < 200 || r.StatusCode >= 300)) {
		return fmt.Errorf("connect: LocalAPI %s returned HTTP %d", req.Method, r.StatusCode)
	}
	// Even a successful header does not complete a response whose body stalls.
	if _, err = io.Copy(io.Discard, io.LimitReader(r.Body, 2*1024*1024)); err != nil {
		return fmt.Errorf("connect: LocalAPI %s response failed", req.Method)
	}
	return nil
}
