package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		var e *exitError
		if errors.As(err, &e) {
			os.Exit(e.code)
		}
		os.Exit(1)
	}
}

type exitError struct{ code int }

func (e *exitError) Error() string { return fmt.Sprintf("command exited %d", e.code) }
func emit(v any) error             { return json.NewEncoder(os.Stdout).Encode(v) }
func run(a []string) error {
	if len(a) == 0 {
		return errors.New("command required")
	}
	switch a[0] {
	case "atomic-link":
		if len(a) != 3 {
			return errors.New("atomic-link TARGET LINK")
		}
		return atomicLink(a[1], a[2])
	case "log":
		if len(a) != 3 {
			return errors.New("log PATH MAX_BYTES")
		}
		n, e := strconv.ParseInt(a[2], 10, 64)
		if e != nil || n < 65536 || n > 4*1024*1024 {
			return errors.New("invalid log limit")
		}
		return logStream(os.Stdin, a[1], n)
	case "quote":
		if len(a) != 2 {
			return errors.New("quote STRING")
		}
		return emit(a[1])
	case "json-get":
		if len(a) != 3 {
			return errors.New("json-get FILE FIELD")
		}
		b, e := os.ReadFile(a[1])
		if e != nil {
			return e
		}
		var v any
		if e = json.Unmarshal(b, &v); e != nil {
			return e
		}
		for _, k := range strings.Split(a[2], ".") {
			switch x := v.(type) {
			case map[string]any:
				var ok bool
				v, ok = x[k]
				if !ok {
					return errors.New("field missing")
				}
			case []any:
				i, e := strconv.Atoi(k)
				if e != nil || i < 0 || i >= len(x) {
					return errors.New("index missing")
				}
				v = x[i]
			default:
				return errors.New("field missing")
			}
		}
		if s, ok := v.(string); ok {
			fmt.Println(s)
			return nil
		}
		return emit(v)
	case "timeout":
		if len(a) < 3 {
			return errors.New("timeout SECONDS COMMAND...")
		}
		n, e := strconv.Atoi(a[1])
		if e != nil || n < 1 || n > 1800 {
			return errors.New("invalid deadline")
		}
		return deadlineCommand(time.Duration(n)*time.Second, a[2:], nil, os.Stdout, os.Stderr)
	case "version":
		if len(a) != 2 {
			return errors.New("version BINARY")
		}
		var out strings.Builder
		if e := deadlineCommand(5*time.Second, []string{a[1], "version"}, append(os.Environ(), "TS_BE_CLI=1"), &out, io.Discard); e != nil {
			return e
		}
		fields := strings.Fields(out.String())
		if len(fields) == 0 || !versionRE.MatchString(fields[0]) {
			return errors.New("invalid core version")
		}
		fmt.Println(fields[0])
		return nil
	case "status":
		if len(a) != 2 {
			return errors.New("status SOCKET")
		}
		return emit(readStatus(a[1]))
	case "fetch":
		if len(a) != 4 {
			return errors.New("fetch URL DEST MAX_BYTES")
		}
		n, e := strconv.ParseInt(a[3], 10, 64)
		if e != nil || n < 1 || n > maxArchive {
			return errors.New("invalid download limit")
		}
		return fetch(a[1], a[2], n)
	case "verify":
		if len(a) != 4 {
			return errors.New("verify ENVELOPE PUBKEY ARCH")
		}
		d, e := verifyFile(a[1], a[2], a[3])
		if e != nil {
			return e
		}
		return emit(d)
	case "extract":
		if len(a) != 4 {
			return errors.New("extract ARCHIVE DESCRIPTOR DEST")
		}
		return extract(a[1], a[2], a[3])
	case "keygen":
		if len(a) != 3 {
			return errors.New("keygen PRIVATE PUBLIC")
		}
		return keygen(a[1], a[2])
	case "sign":
		if len(a) != 4 {
			return errors.New("sign PAYLOAD PRIVATE ENVELOPE")
		}
		return signFile(a[1], a[2], a[3])
	case "compare":
		if len(a) != 3 {
			return errors.New("compare VERSION VERSION")
		}
		n, e := compareVersion(a[1], a[2])
		if e != nil {
			return e
		}
		fmt.Println(n)
		return nil
	case "cidr":
		if len(a) != 3 {
			return errors.New("cidr IP NETMASK")
		}
		s, e := ipv4CIDR(a[1], a[2])
		if e != nil {
			return e
		}
		fmt.Println(s)
		return nil
	}
	return errors.New("unknown command")
}
func deadlineCommand(d time.Duration, args, env []string, out, errOut io.Writer) error {
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Stdout = out
	cmd.Stderr = errOut
	cmd.Env = env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if e := cmd.Start(); e != nil {
		return e
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case e := <-done:
		if e == nil {
			return nil
		}
		var ex *exec.ExitError
		if errors.As(e, &ex) {
			n := ex.ExitCode()
			if n < 0 {
				n = 1
			}
			return &exitError{n}
		}
		return e
	case <-timer.C:
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-done:
		case <-time.After(500 * time.Millisecond):
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-done
		}
		return &exitError{124}
	}
}
