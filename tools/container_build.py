#!/usr/bin/env python3
"""Container-only recipe; network is used solely for go.sum-verified modules."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

arch, host, source_name, version, commit, recipe_sha256, jobs = sys.argv[1:]
recipe = json.loads(Path("/repo/tools/core_recipe.json").read_text())
toolchain = Path("/cache/toolchains") / host
go = str(toolchain / "go/bin/go")
upx = str(toolchain / f'upx-{recipe["upx_version"]}-{host}_linux/upx')
env = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH=arch, GOARM="7", GOARM64="v8.0",
           GOTOOLCHAIN="local", GOENV="off", GOTELEMETRY="off", GOMODCACHE="/cache/gomod", GOCACHE="/cache/gobuild-" + arch,
           GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org", SOURCE_DATE_EPOCH="0")
output = Path("/out/cores") / arch
output.mkdir(parents=True, exist_ok=True)
binary = output / "tailscale.combined"
flags = (f"-s -w -buildid= -X tailscale.com/version.shortStamp={version} "
         f"-X tailscale.com/version.longStamp={version}-t{commit[:9]} "
         f"-X tailscale.com/version.gitCommitStamp={commit}")
subprocess.run([go, "build", "-mod=readonly", "-buildvcs=false", "-trimpath", "-p", jobs,
                "-tags=" + ",".join(recipe["tags"]), "-ldflags=" + flags,
                "-o", str(binary), "./cmd/tailscaled"],
               cwd="/cache/source/" + source_name, env=env, check=True)
raw_size = binary.stat().st_size
raw_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
subprocess.run([upx, *recipe["upx_args"], str(binary)], check=True)
subprocess.run([upx, "--test", str(binary)], check=True)
if binary.stat().st_size > recipe["max_binary_size"]:
    raise RuntimeError("combined binary exceeds 12 MiB gate")
binary.chmod(0o755)
result = {"arch": arch, "version": version, "build": recipe["build"], "source_commit": commit,
          "recipe_sha256": recipe_sha256, "uncompressed_size": raw_size, "uncompressed_sha256": raw_sha,
          "binary_size": binary.stat().st_size, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
(output / "build.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
print(json.dumps(result), flush=True)
