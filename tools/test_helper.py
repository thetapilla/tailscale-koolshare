#!/usr/bin/env python3
"""Run helper tests using its own verified toolchain, including Unix sockets."""
from pathlib import Path
import os
import subprocess

from build_helper import cache_key, helper_recipe, host_arch, prepare_helper_toolchain, provision_helper_go
from toolchain import toolchain_dir

ROOT = Path(__file__).resolve().parents[1]


def main():
    host, recipe = host_arch(), helper_recipe(ROOT)
    lock = prepare_helper_toolchain(ROOT)
    provision_helper_go(ROOT, host, lock)
    go = toolchain_dir("/cache", "helper", lock, host) / "go/bin/go"
    subprocess.run(["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "--mount", f"type=bind,src={ROOT / 'cmd'},dst=/repo/cmd,readonly",
                    "--mount", f"type=bind,src={ROOT / 'go.mod'},dst=/repo/go.mod,readonly",
                    "--mount", f"type=bind,src={ROOT / '.cache'},dst=/cache",
                    "-w", "/repo", "-e", "GOCACHE=/cache/helper-test/" + cache_key(lock),
                    "-e", "GOMODCACHE=/cache/helper-gomod", "-e", "GOTOOLCHAIN=local",
                    "-e", "GOENV=off", "-e", "GOTELEMETRY=off",
                    recipe["container"], str(go), "test", "-v", "./cmd/tsks-helper"], check=True)


if __name__ == "__main__":
    main()
