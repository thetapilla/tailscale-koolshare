#!/usr/bin/env python3
"""Exercise plugin replies through extracted firmware's real HTTP/DBus services.

All writable state, callbacks and mocked network operations live in an ephemeral
container. Firmware binaries, shell applets and the Go helper are executed; WAN,
firewall, core downloads and Tailscale LocalAPI data use deterministic fixtures.
"""
import argparse
import base64
import concurrent.futures
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]


def host_relay(container, port):
    """Use one multiplexed Docker exec channel; firmware has no WAN network."""
    worker = """import base64,concurrent.futures,json,sys,threading,urllib.request,urllib.error
output_lock=threading.Lock()
def handle(request):
 try:
  data=None if request['body'] is None else base64.b64decode(request['body'])
  target=urllib.request.Request('http://127.0.0.1:3030'+request['path'],data=data,headers={'Content-Type':request['content_type']})
  try: response=urllib.request.urlopen(target,timeout=15)
  except urllib.error.HTTPError as error: response=error
  with response:
   result={'status':response.status,'content_type':response.headers.get('Content-Type','application/json'),'body':base64.b64encode(response.read()).decode()}
 except Exception as error: result={'error':str(error)}
 result['id']=request['id']
 with output_lock: print(json.dumps(result),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
 for line in sys.stdin: pool.submit(handle,json.loads(line))
"""
    class Bridge:
        def __init__(self):
            self.lock = threading.Lock()
            self.process = None
            self.pending = {}
            self.next_id = 0

        def reader(self, process):
            try:
                for line in process.stdout:
                    response = json.loads(line)
                    with self.lock:
                        future = self.pending.pop(response["id"], None)
                    if future:
                        future.set_result(response)
            finally:
                with self.lock:
                    if self.process is process:
                        self.process = None
                        for future in self.pending.values():
                            future.set_exception(OSError("fixture relay channel closed"))
                        self.pending.clear()

        def request(self, request):
            with self.lock:
                if self.process is None or self.process.poll() is not None:
                    for future in self.pending.values():
                        future.set_exception(OSError("fixture relay channel restarted"))
                    self.pending.clear()
                    self.process = subprocess.Popen(["docker", "exec", "-i", container, "/usr/local/bin/python3", "-u", "-c", worker],
                                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
                    threading.Thread(target=self.reader, args=(self.process,), daemon=True).start()
                self.next_id += 1
                request["id"] = self.next_id
                future = concurrent.futures.Future()
                self.pending[self.next_id] = future
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
            try:
                response = future.result(timeout=20)
                if "error" in response:
                    raise OSError(response["error"])
                return response
            finally:
                with self.lock:
                    self.pending.pop(request["id"], None)

        def close(self):
            with self.lock:
                process = self.process
                if process and process.poll() is None:
                    process.stdin.close()
            if process:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    bridge = Bridge()
    class Relay(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def forward(self):
            if not self.path.startswith(("/_api/", "/_temp/")):
                self.send_error(404)
                return
            data = self.rfile.read(int(self.headers.get("Content-Length", 0))) if self.command == "POST" else None
            request = {"path": self.path, "body": None if data is None else base64.b64encode(data).decode(),
                       "content_type": self.headers.get("Content-Type", "application/json")}
            try:
                response = bridge.request(request)
                body = base64.b64decode(response["body"])
                self.send_response(response["status"])
                self.send_header("Content-Type", response["content_type"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError, subprocess.SubprocessError):
                try:
                    self.send_error(502)
                except OSError:
                    pass

        do_GET = forward
        do_POST = forward
    class RelayServer(http.server.ThreadingHTTPServer):
        def server_close(self):
            bridge.close()
            super().server_close()
    return RelayServer(("127.0.0.1", port), Relay)


def write_executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def inside(serve, ids_only=False):
    sys.path.insert(0, "/tests")
    import test_backend

    firmware = Path("/firmware")
    machine = int.from_bytes((firmware / "bin/busybox").read_bytes()[18:20], "little")
    qemu = "/usr/bin/qemu-arm-static" if machine == 40 else "/usr/bin/qemu-aarch64-static"
    prefix = [qemu, "-L", "/firmware"]
    fixture = Path("/fixture")
    fixture.mkdir()
    for path in ("/jffs/db", "/koolshare/bin", "/koolshare/scripts", "/koolshare/tailscale/current",
                 "/koolshare/configs/tailscale", "/tmp/tailscale3", "/tmp/upload", "/fixture/proc", "/fixture/bin"):
        Path(path).mkdir(parents=True, exist_ok=True)
    for source in Path("/plugin/scripts").iterdir():
        shutil.copy2(source, Path("/koolshare/scripts") / source.name)
    env = dict(os.environ, TSKS_ROOT="/koolshare", TSKS_RUN="/tmp/tailscale3", TSKS_WEB="/tmp/upload",
               TSKS_PROC="/fixture/proc", TSKS_SYSFS="/fixture/sys", MOCK_ROOT="/fixture",
               PATH="/koolshare/bin:/koolshare/scripts:/fixture/bin")
    os.environ.update(env)

    # Every plugin utility runs the firmware's applet. The daemon invokes its
    # script method by name through /bin/sh, so both PATH and that shell matter.
    shell_wrapper = "#!/usr/local/bin/python3\nimport os,sys\nfrom pathlib import Path\n" + \
        "name=Path(sys.argv[0]).name\nos.execv(" + repr(qemu) + "," + repr(prefix + ["/firmware/bin/busybox"]) + "+[name]+sys.argv[1:])\n"
    applets = subprocess.check_output(prefix + ["/firmware/bin/busybox", "--list"], text=True).splitlines()
    for name in applets:
        write_executable(Path("/fixture/bin") / name, shell_wrapper)
    # Replace only the disposable container's shell; firmware remains read-only.
    Path("/bin/sh").unlink()
    write_executable(Path("/bin/sh"), shell_wrapper)

    mock = fixture / "mock-command"
    write_executable(mock, test_backend.MOCK.replace("#!/usr/bin/env python3", "#!/usr/local/bin/python3"))
    for name in ("nvram", "cru", "iptables", "ip6tables", "iptables-save", "ip6tables-save"):
        (Path("/koolshare/bin") / name).symlink_to(mock)
    write_executable(Path("/koolshare/bin/dbus"), "#!/usr/local/bin/python3\nimport os,sys\nos.execv(" + repr(qemu) + "," + repr(prefix + ["/firmware/usr/bin/dbus"]) + "+sys.argv[1:])\n")
    write_executable(Path("/koolshare/bin/curl"), """#!/usr/local/bin/python3
import os,sys
if any(a.startswith('http://127.0.0.1:3030/_resp/') for a in sys.argv[1:]):
    os.execv('/usr/bin/curl',['curl']+sys.argv[1:])
sys.exit(7)
""")
    write_executable(Path("/koolshare/bin/tsks-helper"), "#!/usr/local/bin/python3\nimport os,sys\n" +
                     "if sys.argv[1:2]==['fetch']:sys.exit(1)\n" +
                     "os.execv(" + repr(qemu) + "," + repr([qemu, "/helper/tsks-helper"]) + "+sys.argv[1:])\n")
    for name in ("tailscale", "tailscaled", "tailscale.combined"):
        (Path("/koolshare/tailscale/current") / name).symlink_to(mock)
    (fixture / "nvram.json").write_text(json.dumps({"lan_ifname": "br0", "lan_ipaddr": "192.0.2.1", "lan_netmask": "255.255.255.0",
                                                     "wan_primary": "0", "wan0_state_t": "0", "wan0_link": "0"}))
    (fixture / "proc/meminfo").write_text("MemAvailable: 131072 kB\nMemFree: 131072 kB\n")
    (fixture / "proc/uptime").write_text("3600.00 0.00\n")
    (fixture / "cli_output.json").write_text(json.dumps('100.64.0.1 fixture "device" active\n'))
    (Path("/koolshare/tailscale/current/descriptor.json")).write_text(json.dumps({"version": "1.102.4", "arch": "arm" if machine == 40 else "arm64"}))
    Path("/koolshare/tailscale/release.pub").write_text("fixture-public-key")
    Path("/koolshare/configs/tailscale/tailscaled.state").write_text("private-fixture-identity")

    health_message = '诊断 "引用" \\ 路径\r\n下一行\t<script>&'
    class LocalAPI(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/localapi/v0/status"):
                body = {"Version": "1.102.4", "BackendState": "Running", "Health": [health_message], "AuthURL": "",
                        "HaveNodeKey": True, "Self": {"ID": "fixture-node", "Online": True, "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"]}}
            elif self.path == "/localapi/v0/prefs":
                body = {"WantRunning": True, "LoggedOut": False}
            elif self.path.startswith("/localapi/v0/watch-ipn-bus"):
                body = {"Health": {"Warnings": {}}}
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = socketserver.UnixStreamServer("/tmp/tailscale3/tailscaled.sock", LocalAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    handles = []
    children = []
    for name, command in (("skipd", prefix + ["/firmware/usr/bin/skipd"]),
                          ("httpdb", prefix + ["/firmware/rom/etc/koolshare/bin/httpdb"])):
        log = (fixture / (name + ".log")).open("w")
        handles.append(log)
        children.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env))
        if name == "skipd":
            for _ in range(100):
                if Path("/tmp/.skipd_server_sock").exists():
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("firmware database socket did not become ready")
            for key, value in {"tailscale_enable": "1", "tailscale_watchdog_enable": "1", "tailscale_ipv4_enable": "1",
                               "tailscale_ipv6_enable": "1", "tailscale_advertise_routes": "1", "tailscale_accept_routes": "1",
                               "tailscale_exit_node": "0", "tailscale_version": "3.0.0"}.items():
                subprocess.run(["/koolshare/bin/dbus", "set", key + "=" + value], check=True, timeout=5, stdout=subprocess.DEVNULL)

    def request_http(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request("http://127.0.0.1:3030" + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
        return raw

    for _ in range(100):
        try:
            config = json.loads(request_http("/_api/tailscale_"))["result"][0]
            if config["tailscale_enable"] == "1":
                break
        except (OSError, ValueError, KeyError):
            time.sleep(.05)
    else:
        raise RuntimeError("firmware HTTP configuration endpoint did not become ready")

    next_id = 1000
    timings = {}
    job_timings = {}
    def rpc(method, params=(), request_id=None):
        nonlocal next_id
        next_id += 1
        request_id = next_id if request_id is None else request_id
        started = time.monotonic()
        raw = request_http("/_api/", {"id": request_id, "method": method, "params": list(params), "fields": {}})
        timings.setdefault(method, []).append((time.monotonic() - started, len(raw)))
        outer = json.loads(raw)
        if not isinstance(outer.get("result"), str):
            raise AssertionError((method, raw))
        result = json.loads(outer["result"])
        return request_id, result

    def job(method, params=(), expected="success"):
        request_id, reply = rpc(method, params)
        assert reply == {"accepted": True, "job_id": str(request_id)}, (method, reply)
        started = time.monotonic()
        deadline = started + 60
        while time.monotonic() < deadline:
            _, result = rpc("tailscale_job", [str(request_id)])
            assert result["id"] == str(request_id)
            if result["state"] != "running":
                assert result["state"] == expected, (method, result)
                job_timings.setdefault(method, []).append(round(time.monotonic() - started, 3))
                return result
            time.sleep(.1)
        raise AssertionError("job did not reach a terminal state: " + method)

    try:
        if Path("/plugin/manifest.sha256").is_file():
            subprocess.run(["/koolshare/bin/tsks-helper", "check-tree", "/plugin"], check=True, timeout=20)
        else:
            tree = fixture / "package-tree"
            tree.mkdir()
            (tree / "file").write_bytes(b"package check fixture\n")
            (tree / "manifest.sha256").write_text(hashlib.sha256((tree / "file").read_bytes()).hexdigest() + "  file\n")
            subprocess.run(["/koolshare/bin/tsks-helper", "check-tree", str(tree)], check=True, timeout=10)
        # Exercise the actual runtime hash path even when the firmware has no
        # checksum applet. A tiny regular executable isolates version probing.
        probe = fixture / "valid-core"
        probe.mkdir()
        binary = probe / "tailscale.combined"
        write_executable(binary, "#!/bin/sh\nprintf '1.102.4\\n'\n")
        (probe / "descriptor.json").write_text(json.dumps({"version": "1.102.4", "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}))
        subprocess.run(["/bin/sh", "-c", '. /koolshare/scripts/tailscale_lib.sh; ts_init; . /koolshare/scripts/tailscale_core_lib.sh; ts_core_valid /fixture/valid-core'],
                       env=env, check=True, timeout=10)
        _, boundary = rpc("tailscale_fettle", request_id=99999999)
        assert boundary["schema"] == 1 and boundary["backend_state"] == "Running", boundary
        rejected = json.loads(request_http("/_api/", {"id": 100000000, "method": "tailscale_fettle", "params": [], "fields": {}}))
        assert rejected == {"result": 0}, rejected
        _, legacy_id = rpc("tailscale_job", ["123456789"])
        assert legacy_id["id"] == "123456789" and legacy_id["operation_busy"] is False, legacy_id
        if ids_only:
            print(json.dumps({"ok": True, "mode": "id-boundaries", "httpdb_sha256": hashlib.sha256((firmware / "rom/etc/koolshare/bin/httpdb").read_bytes()).hexdigest(),
                              "max_request_id": 99999999, "rejected_request_id": 100000000, "legacy_job_id": "123456789"}), flush=True)
            return
        _, status = rpc("tailscale_fettle")
        assert status["schema"] == 1 and status["backend_state"] == "Running", status
        assert status["health_messages"] == [health_message], status
        original_health = health_message
        health_message += "x" * 8192
        _, large = rpc("tailscale_fettle")
        assert large["health_messages"] == [health_message], "health response was truncated"
        health_message = original_health
        _, interfaces = rpc("tailscale_tsnets", [1])
        assert len(interfaces["interfaces"]) == 2, interfaces
        _, missing = rpc("tailscale_job", ["999999"])
        assert missing["state"] == "unknown" and missing["operation_busy"] is False, missing
        _, invalid = rpc("tailscale_config", ["invalid"])
        assert invalid == {"accepted": False, "error": "invalid_action"}
        import fcntl
        with open("/tmp/tailscale3/operation.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _, busy = rpc("tailscale_core", ["check"])
            assert busy == {"accepted": False, "error": "busy"}, busy
            _, pending = rpc("tailscale_job", ["999999"])
            assert pending["state"] == "unknown" and pending["operation_busy"] is True, pending
        job("tailscale_status")
        job("tailscale_ncheck")
        job("tailscale_diagnostics")
        for operation in ("check", "update", "rollback"):
            job("tailscale_core", [operation], "failed")
        # The deterministic disabled configuration exercises writes and the
        # normal lock/job path without starting a real network daemon.
        job("tailscale_config", ["web_submit", "0111101"])
        config = json.loads(request_http("/_api/tailscale_"))["result"][0]
        assert config["tailscale_enable"] == "0", config

        # The preceding stop removes the socket, providing the real missing-
        # daemon path. Recreate only the test LocalAPI service afterwards.
        _, unavailable = rpc("tailscale_fettle")
        assert unavailable["backend_state"] == "Unavailable" and unavailable["error"] == "local_api_unavailable", unavailable
        _, no_interfaces = rpc("tailscale_tsnets", [1])
        assert no_interfaces == {"interfaces": []}, no_interfaces
        server.shutdown()
        server.server_close()
        server = socketserver.UnixStreamServer("/tmp/tailscale3/tailscaled.sock", LocalAPI)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        values = [{"zero": 0, "one": 1, "enabled": False, "null": None},
                  {"message": health_message, "long": "x" * 8192}]
        for value in values:
            (fixture / "wire.json").write_text(json.dumps(value, ensure_ascii=False))
            write_executable(Path("/koolshare/scripts/tailscale_wire_probe"), '#!/bin/sh\n. /koolshare/scripts/tailscale_lib.sh\nts_init || exit 1\nID=$1\nts_reply "$(cat /fixture/wire.json)"\n')
            _, actual = rpc("tailscale_wire_probe")
            assert actual == value, actual
            console = subprocess.check_output(["/bin/sh", "-c", '. /koolshare/scripts/tailscale_lib.sh; ts_init; ID=; ts_reply "$(cat /fixture/wire.json)"'], env=env, text=True)
            assert json.loads(console) == value
        for log in Path("/tmp/upload").glob("tailscale3_*.log"):
            assert "private-fixture-identity" not in log.read_text()
        summary = {method: {"max_seconds": round(max(x[0] for x in values), 3), "max_bytes": max(x[1] for x in values)} for method, values in timings.items()}
        assert all(item["max_seconds"] < 10 for item in summary.values()), summary
        print(json.dumps({"ok": True, "httpdb_sha256": hashlib.sha256((firmware / "rom/etc/koolshare/bin/httpdb").read_bytes()).hexdigest(),
                          "elf_machine": machine, "requests": next_id - 1000, "latency": summary,
                          "job_seconds": job_timings,
                          "transport": "actual firmware HTTP/DBus and BusyBox; real Go helper"}), flush=True)
        if serve:
            print("HTTPDB_READY", flush=True)
            while True:
                time.sleep(1)
    except Exception:
        for name in ("httpdb.log", "skipd.log", "calls.jsonl"):
            path = fixture / name
            if path.exists():
                print(name + ": " + path.read_text(errors="replace")[-2000:], file=sys.stderr, flush=True)
        print("job records: " + repr([(p.name, p.read_text()) for p in Path("/tmp/upload").glob("*.json")]), file=sys.stderr, flush=True)
        raise
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
        server.shutdown()
        for handle in handles:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-root", type=Path, action="append")
    parser.add_argument("--plugin-root", type=Path, default=ROOT / "plugin", help="plugin source or an unpacked package's tailscale directory")
    parser.add_argument("--image", default="tailscale-firmware-test:bookworm")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--ids-only", action="store_true", help="only verify actual transport request-ID boundaries")
    parser.add_argument("--port", type=int, default=33030)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        inside(args.serve, args.ids_only)
        return
    roots = args.firmware_root or sorted((ROOT / "build/firmware").glob("*/root"))
    if not roots or (args.serve and len(roots) != 1):
        parser.error("provide extracted firmware roots; --serve requires exactly one")
    for firmware in roots:
        firmware = firmware.resolve()
        machine = int.from_bytes((firmware / "bin/busybox").read_bytes()[18:20], "little")
        if machine not in (40, 183):
            raise ValueError("unsupported firmware ELF architecture")
        arch = "arm" if machine == 40 else "arm64"
        name = "tsks-httpdb-" + uuid.uuid4().hex[:12]
        plugin = args.plugin_root.resolve()
        helper = plugin / "payload" / arch
        if not (helper / "tsks-helper").is_file():
            helper = ROOT / "build/helpers" / arch
        command = ["docker", "run", "--rm", "--name", name, "--label", "tsks.httpdb_fixture=true", "--cap-drop=ALL", "--security-opt=no-new-privileges"]
        command += ["--network=none"]
        for source, destination in ((firmware, "/firmware"), (plugin, "/plugin"), (ROOT / "tests", "/tests"),
                                    (ROOT / "tools", "/tools"), (helper, "/helper")):
            command += ["--mount", f"type=bind,src={source},dst={destination},readonly"]
        command += [args.image, "python3", "/tools/smoke_httpdb.py", "--inside"]
        if args.serve:
            command += ["--serve"]
        if args.ids_only:
            command += ["--ids-only"]
        print("Checking extracted firmware transport:", firmware.parent.name, flush=True)
        relay = None
        try:
            if args.serve:
                relay = host_relay(name, args.port)
                threading.Thread(target=relay.serve_forever, daemon=True).start()
            subprocess.run(command, check=True, timeout=None if args.serve else 600)
        finally:
            if relay:
                relay.shutdown()
                relay.server_close()
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
