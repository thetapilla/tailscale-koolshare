#!/usr/bin/env python3
"""Run shell regression suites with BusyBox binaries extracted from firmware.

Firmware remains local and is mounted read-only. Extraction and executable
probing occur inside a network-isolated container. Hardware services are mocked
by the existing backend, updater and installer fixtures; this checks the actual
firmware shell and applet implementations, not hardware or packet forwarding.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "tailscale-firmware-test:bookworm"
DOCKERFILE = """FROM python:3.12-bookworm@sha256:dbbe4ceb97851e2e5fa83798b239811f871cb743b259ba3563737349f6bcfaa0
RUN apt-get update && apt-get install -y --no-install-recommends squashfs-tools qemu-user-static binutils curl && rm -rf /var/lib/apt/lists/*
"""


def squashfs_offset(path):
    data = path.read_bytes()
    offsets = []
    offset = 0
    while True:
        offset = data.find(b"hsqs", offset)
        if offset < 0:
            break
        if offset + 96 <= len(data):
            fields = struct.unpack("<5I6H8Q", data[offset:offset + 96])
            if (fields[9:11] == (4, 0) and fields[12] <= len(data) - offset
                    and fields[3] in (4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576)):
                offsets.append(offset)
        offset += 1
    if len(offsets) != 1:
        raise ValueError(f"expected one SquashFS v4 filesystem; found {len(offsets)}")
    return offsets[0], hashlib.sha256(data).hexdigest()


def source_snapshot():
    return {str(path.relative_to("/repo")): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in ("/repo/plugin", "/repo/tests")
            for path in sorted(Path(directory).rglob("*"))
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts}


def container_run(offset, output, inventory_only=False, export_runtime=False, suites=("backend", "core", "install"), matches=()):
    root = Path("/tmp/firmware-root")
    result = subprocess.run(["unsquashfs", "-no-progress", "-no-xattrs", "-p", "2", "-d", str(root),
                             "-o", str(offset), "/firmware-image", "bin", "sbin", "usr", "lib", "rom", "etc", "www"],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    (output / "extract.log").write_text(result.stdout)
    if result.returncode:
        raise RuntimeError("firmware extraction failed; see extract.log")
    busybox = root / "bin/busybox"
    elf = busybox.read_bytes()
    if elf[:4] != b"\x7fELF" or elf[5] != 1:
        raise ValueError("unsupported BusyBox executable")
    arch = {40: "arm", 183: "aarch64"}.get(int.from_bytes(elf[18:20], "little"))
    if not arch:
        raise ValueError("expected ARM or AArch64 firmware")
    emulator = f"/usr/bin/qemu-{arch}-static"
    command = [emulator, "-L", str(root), str(busybox)]
    listing = subprocess.check_output([*command, "--list"], text=True, timeout=10)
    (output / "applets.txt").write_text(listing)
    version = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10).stdout.splitlines()[0]
    hashes = {}
    for name in ("bin/busybox", "usr/bin/dbus", "usr/bin/skipd", "usr/sbin/httpd", "usr/sbin/curl", "usr/sbin/openssl",
                 "rom/etc/koolshare/bin/httpdb", "rom/etc/koolshare/scripts/base.sh",
                 "rom/etc/koolshare/res/softcenter.js"):
        file = root / name
        if file.is_file():
            hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    info = {"architecture": arch, "busybox": version, "hashes": hashes, "applets": listing.splitlines(),
            "emulator": subprocess.check_output([emulator, "--version"], text=True).splitlines()[0]}
    info["shell_builtins"] = subprocess.run(
        [*command, "ash", "-c", "for name in echo printf test '[' kill command local true false; do type \"$name\"; done"],
        env=dict(os.environ, PATH="/nonexistent"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=10).stdout.splitlines()
    if export_runtime:
        exported = output / "runtime-root"
        if exported.is_symlink():
            raise ValueError("runtime output must not be a symlink")
        if exported.exists():
            for directory, dirs, files in os.walk(exported):
                os.chmod(directory, 0o700)
            shutil.rmtree(exported)
        exported.mkdir(exist_ok=True)
        for name in ("bin", "lib", "usr/lib", "usr/bin/dbus", "usr/bin/skipd", "usr/sbin/curl", "usr/sbin/openssl",
                     "usr/sbin/iptables", "usr/sbin/ip6tables", "usr/sbin/xtables-multi",
                     "rom/etc/koolshare", "www/js/jquery.js"):
            source, target = root / name, exported / name
            if source.is_dir() and not source.is_symlink():
                def exclude(directory, names):
                    if Path(directory) == root / "lib":
                        return {"modules"}
                    if Path(directory) == root / "usr/lib":
                        # Export dynamic link dependencies, not optional
                        # extension trees with case-sensitive match/target names.
                        return {name for name in names if (Path(directory) / name).is_dir()}
                    return set()
                shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True,
                                ignore=exclude)
            elif source.exists() or source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.is_file():
                    target.unlink()
                shutil.copy2(source, target, follow_symlinks=False)
        info["exported_root"] = "runtime-root"
    (output / "runtime.json").write_text(json.dumps(info, indent=2) + "\n")
    probes = {}
    external_commands = {}
    openssl = root / "usr/sbin/openssl"
    if openssl.is_file():
        canonical = Path("/usr/sbin/openssl")
        shutil.copy2(openssl, canonical)
        external_commands["openssl"] = [emulator, "-L", str(root), str(canonical)]
        sample = Path("/tmp/firmware-hash-input")
        sample.write_bytes(b"abc")
        result = subprocess.run([*external_commands["openssl"], "dgst", "-sha256", str(sample)],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        digest = result.stdout.split()[-1:] == [hashlib.sha256(b"abc").hexdigest()]
        probes["openssl dgst -sha256"] = {"returncode": result.returncode, "output": result.stdout,
                                          "digest_verified": result.returncode == 0 and digest}
        if not probes["openssl dgst -sha256"]["digest_verified"]:
            raise RuntimeError("firmware OpenSSL cannot verify SHA-256")
    callback_bytes = b'{\\"schema\\":1,\\"message\\":\\"callback test\\"}'
    callbacks = []
    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            callbacks.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    result = subprocess.run([*command, "df", "-Pk", "/tmp"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=10)
    probes["busybox df -Pk"] = {"returncode": result.returncode, "output": result.stdout}
    rows = result.stdout.splitlines()
    if result.returncode or len(rows) < 2 or len(rows[-1].split()) < 4 or not rows[-1].split()[3].isdigit():
        raise RuntimeError("firmware df output is incompatible with space preflight")
    for name in ("usr/sbin/curl", "usr/bin/curl", "usr/sbin/iptables", "usr/sbin/ip6tables",
                 "sbin/iptables", "sbin/ip6tables"):
        file = root / name
        if file.is_file():
            executable = file
            if name == "usr/sbin/curl":
                # Some vendor builds require this canonical executable path.
                # /usr/sbin is a disposable tmpfs; firmware bytes stay read-only.
                executable = Path("/usr/sbin/curl")
                shutil.copy2(file, executable)
            result = subprocess.run([emulator, "-L", str(root), str(executable),
                                     "--version" if name.endswith("/curl") else "--help"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            probes[name] = {"returncode": result.returncode, "output": result.stdout}
            if name.endswith("/curl"):
                callbacks.clear()
                with http.server.HTTPServer(("127.0.0.1", 0), Callback) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        result = subprocess.run([emulator, "-L", str(root), str(executable), "--noproxy", "*", "-fsS",
                                                 "--connect-timeout", "2", "--max-time", "5", "-X", "POST",
                                                 "--data-binary", callback_bytes.decode(),
                                                 f"http://127.0.0.1:{server.server_port}/_resp/1"], text=True,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                    finally:
                        server.shutdown()
                        thread.join(timeout=2)
                probes[name]["callback_post_verified"] = result.returncode == 0 and callbacks == [callback_bytes]
                probes[name]["callback_returncode"] = result.returncode
                probes[name]["callback_output"] = result.stdout
    (output / "utility-probes.json").write_text(json.dumps(probes, indent=2) + "\n")
    with open("/tmp/firmware-flock", "w") as first, open("/tmp/firmware-flock", "r+") as second:
        for option, stream, expected in (("-n", first, 0), ("-n", second, 1),
                                         ("-u", first, 0), ("-n", second, 0)):
            code = subprocess.run([*command, "flock", option, str(stream.fileno())],
                                  pass_fds=(stream.fileno(),), timeout=10).returncode
            if code != expected:
                raise RuntimeError("firmware flock descriptor locking semantics differ")
    if inventory_only:
        return 0
    print(version + " / " + arch, flush=True)
    wrapper = Path("/tmp/firmware-commands")
    wrapper.mkdir()
    # Test fixtures symlink individual applets to this dispatcher. The firmware
    # executable supplies both the shell parser and the real applet options.
    script = wrapper / "busybox"
    dispatch = "#!/bin/sh\nname=${0##*/}\ncase $name in\n"
    for name, arguments in external_commands.items():
        dispatch += name + ") exec " + shlex.join(arguments) + ' "$@";;\n'
    dispatch += 'esac\n[ "$name" = busybox ] || set -- "$name" "$@"\nexec ' + shlex.join(command) + ' "$@"\n'
    script.write_text(dispatch)
    script.chmod(0o755)
    (wrapper / "sh").symlink_to(script)
    for name in external_commands:
        (wrapper / name).symlink_to(script)
    for file in [*Path("/repo/plugin/scripts").glob("*"), *Path("/repo/plugin/init.d").glob("*"),
                 Path("/repo/plugin/install.sh"), Path("/repo/plugin/uninstall.sh")]:
        subprocess.run([*command, "ash", "-n", str(file)], check=True, timeout=10)
    available = set(listing.splitlines()) | set(external_commands)
    env = dict(os.environ, TSKS_TEST_SHELL=str(wrapper / "sh"), TSKS_TEST_BUSYBOX=str(script),
               TSKS_FIRMWARE_COMMANDS=json.dumps(sorted(available)),
               TSKS_TEST_OPENSSL=str(wrapper / "openssl"))
    # Vendor ash may compile even echo, printf, test and kill as external
    # applets. The normal fixture enables them as builtins, so add their real
    # firmware implementations to each fixture's isolated utility directory.
    external_builtins = [name for name in ("[", "echo", "printf", "test", "kill", "true", "false")
                         if name in listing.splitlines()]
    runner = r'''
import sys, unittest, os, json
available = set(json.loads(os.environ["TSKS_FIRMWARE_COMMANDS"]))
count = int(sys.argv[2])
unittest.defaultTestLoader.testNamePatterns = sys.argv[3:3 + count] or None
suite = unittest.defaultTestLoader.discover("tests", pattern=sys.argv[1])
for module in tuple(sys.modules.values()):
    if str(getattr(module, "__file__", "")).startswith("/repo/tests/") and hasattr(module, "FIRMWARE_TOOLS"):
        module.FIRMWARE_TOOLS = tuple(dict.fromkeys(name for name in (*module.FIRMWARE_TOOLS, *sys.argv[3 + count:]) if name in available))
if suite.countTestCases() == 0:
    raise SystemExit("no firmware tests selected")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
'''
    status = 0
    results = {}
    for suite in suites:
        before = source_snapshot()
        with (output / f"{suite}.log").open("w") as log:
            result = subprocess.run([sys.executable, "-c", runner, f"test_{suite}.py", str(len(matches)), *matches, *external_builtins],
                                    env=env, cwd="/repo", stdout=log, stderr=subprocess.STDOUT, timeout=900)
        print(f"{suite}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
        results[suite] = {"returncode": result.returncode, "source_before": before, "source_after": source_snapshot()}
        status |= result.returncode
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="*", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "build/firmware-compat")
    parser.add_argument("--build-image", action="store_true")
    parser.add_argument("--jobs", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--export-runtime", action="store_true",
                        help="export the minimal firmware runtime beside local test reports")
    parser.add_argument("--suite", choices=("all", "backend", "core", "install"), default="all")
    parser.add_argument("--match", action="append", default=[], help="unittest test-name glob (repeatable)")
    parser.add_argument("--container-offset", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.container_offset is not None:
        suites = ("backend", "core", "install") if args.suite == "all" else (args.suite,)
        return container_run(args.container_offset, Path("/output"), args.inventory_only, args.export_runtime, suites, args.match)
    if not args.images:
        parser.error("provide local firmware image paths")
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    if args.build_image:
        with tempfile.TemporaryDirectory(prefix="tsks-firmware-build-") as temp:
            (Path(temp) / "Dockerfile").write_text(DOCKERFILE)
            subprocess.run(["docker", "build", "-t", IMAGE, temp], check=True)
    def run_image(image):
        image = image.resolve(strict=True)
        offset, digest = squashfs_offset(image)
        target = output / digest[:16]
        target.mkdir(exist_ok=True)
        (target / "source.json").write_text(json.dumps({"filename": image.name, "sha256": digest,
                                                      "squashfs_offset": offset}, indent=2) + "\n")
        print(f"Firmware {digest[:16]}", flush=True)
        name = "tsks-firmware-" + uuid.uuid4().hex[:12]
        try:
            result = subprocess.run([
                "docker", "run", "--rm", "--name", name, "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=768m",
                "--tmpfs", "/usr/sbin:rw,exec,nosuid,size=4m",
                "--mount", f"type=bind,src={image},dst=/firmware-image,readonly",
                "--mount", f"type=bind,src={ROOT / 'plugin'},dst=/repo/plugin,readonly",
                "--mount", f"type=bind,src={ROOT / 'tests'},dst=/repo/tests,readonly",
                "--mount", f"type=bind,src={ROOT / 'tools/firmware_compat.py'},dst=/repo/tools/firmware_compat.py,readonly",
                "--mount", f"type=bind,src={target},dst=/output",
                IMAGE, "python3", "/repo/tools/firmware_compat.py", "--container-offset", str(offset),
                *(["--inventory-only"] if args.inventory_only else []),
                *(["--export-runtime"] if args.export_runtime else []),
                "--suite", args.suite, *[part for pattern in args.match for part in ("--match", pattern)]
            ], timeout=2400)
            return result.returncode
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        return int(any(list(pool.map(run_image, args.images))))


if __name__ == "__main__":
    raise SystemExit(main())
