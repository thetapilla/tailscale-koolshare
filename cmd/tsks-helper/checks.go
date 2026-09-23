package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"regexp"
	"strings"
	"syscall"
)

const maxPackageManifest = 1024 * 1024
const maxPackageEntries = 4096

var packagePathRE = regexp.MustCompile(`^[A-Za-z0-9_./-]+$`)

func regularSHA256(file *os.File) (string, error) {
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", errors.New("checksum source is not a regular file")
	}
	hash := sha256.New()
	if _, err = io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func fileSHA256(name string) (string, error) {
	// Parent pointers such as tailscale/current are valid. The file itself must
	// be regular; nonblocking open also avoids hanging on an unexpected FIFO.
	file, err := os.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return "", err
	}
	defer file.Close()
	return regularSHA256(file)
}

func packagePath(name string) bool {
	if len(name) == 0 || len(name) > 1024 || !packagePathRE.MatchString(name) || strings.HasPrefix(name, "/") {
		return false
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return true
}

func checkTree(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return errors.New("package root is not a directory")
	}
	root, err := os.OpenRoot(path)
	if err != nil {
		return err
	}
	defer root.Close()
	manifest, err := root.OpenFile("manifest.sha256", os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return err
	}
	defer manifest.Close()
	info, err = manifest.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() > maxPackageManifest {
		return errors.New("invalid package manifest file")
	}
	data, err := io.ReadAll(io.LimitReader(manifest, maxPackageManifest+1))
	if err != nil {
		return err
	}
	if len(data) > maxPackageManifest {
		return errors.New("package manifest is too large")
	}
	wanted := make(map[string]string)
	lines := strings.Split(string(data), "\n")
	for index, line := range lines {
		if index == len(lines)-1 && line == "" {
			continue
		}
		for _, char := range line {
			if (char < 32 && char != '\t') || char > 126 {
				return errors.New("invalid package manifest characters")
			}
		}
		fields := strings.Fields(line)
		if len(fields) != 2 || !hex64.MatchString(fields[0]) || !packagePath(fields[1]) || fields[1] == "manifest.sha256" {
			return errors.New("invalid package manifest entry")
		}
		if _, exists := wanted[fields[1]]; exists {
			return errors.New("duplicate package manifest path")
		}
		wanted[fields[1]] = fields[0]
		if len(wanted) > maxPackageEntries {
			return errors.New("too many package manifest entries")
		}
	}
	if len(wanted) == 0 {
		return errors.New("empty package manifest")
	}
	seen := make(map[string]bool)
	entries := 0
	err = fs.WalkDir(root.FS(), ".", func(name string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if name == "." {
			return nil
		}
		entries++
		if entries > maxPackageEntries*2 || !packagePath(name) {
			return errors.New("invalid package tree")
		}
		if entry.IsDir() {
			return nil
		}
		if !entry.Type().IsRegular() {
			return fmt.Errorf("package entry is not a regular file: %s", name)
		}
		if name == "manifest.sha256" {
			return nil
		}
		expected, exists := wanted[name]
		if !exists {
			return fmt.Errorf("unlisted package file: %s", name)
		}
		file, openErr := root.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if openErr != nil {
			return openErr
		}
		actual, hashErr := regularSHA256(file)
		file.Close()
		if hashErr != nil {
			return hashErr
		}
		if actual != expected {
			return fmt.Errorf("package checksum mismatch: %s", name)
		}
		seen[name] = true
		return nil
	})
	if err != nil {
		return err
	}
	if len(seen) != len(wanted) {
		return errors.New("listed package file is missing")
	}
	return nil
}
