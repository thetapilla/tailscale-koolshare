package main

import (
	"bufio"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

func atomicLink(target, path string) error {
	if filepath.IsAbs(target) || strings.Contains(target, "..") || !strings.HasPrefix(target, "cores/") {
		return errors.New("invalid core link target")
	}
	if fi, e := os.Lstat(path); e == nil && fi.Mode()&os.ModeSymlink == 0 {
		return errors.New("core pointer is not a symlink")
	}
	resolved := filepath.Join(filepath.Dir(path), target)
	fi, e := os.Stat(resolved)
	if e != nil || !fi.IsDir() {
		return errors.New("core target does not exist")
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".core-link-*")
	if e != nil {
		return e
	}
	name := f.Name()
	f.Close()
	os.Remove(name)
	defer os.Remove(name)
	if e = os.Symlink(target, name); e != nil {
		return e
	}
	if e = os.Rename(name, path); e != nil {
		return e
	}
	dir, e := os.Open(filepath.Dir(path))
	if e != nil {
		return e
	}
	defer dir.Close()
	return dir.Sync()
}
func logStream(input io.Reader, path string, limit int64) error {
	reader := bufio.NewReaderSize(input, 65536)
	var line []byte
	oversized := false
	for {
		part, e := reader.ReadSlice('\n')
		if len(line)+len(part) <= 65536 && !oversized {
			line = append(line, part...)
		} else {
			oversized = true
			line = nil
		}
		if errors.Is(e, bufio.ErrBufferFull) {
			continue
		}
		if len(line) > 0 || oversized {
			if oversized {
				line = []byte("[oversized log entry omitted]\n")
			}
			clean := []byte(redact(string(line)))
			if st, se := os.Stat(path); se == nil && st.Size()+int64(len(clean)) > limit {
				_ = os.Remove(path + ".1")
				if se = os.Rename(path, path+".1"); se != nil {
					return se
				}
			}
			f, se := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0600)
			if se != nil {
				return se
			}
			_, se = f.Write(clean)
			f.Close()
			if se != nil {
				return se
			}
		}
		line = nil
		oversized = false
		if e != nil {
			if errors.Is(e, io.EOF) {
				return nil
			}
			return e
		}
	}
}
