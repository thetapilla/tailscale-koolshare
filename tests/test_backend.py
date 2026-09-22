"""Isolated backend regression tests. No router or host service access.

Every command with a router side effect is replaced in PATH; all writable roots
are TemporaryDirectory children. Set TSKS_TEST_SHELL to an ash-compatible shell
to run the identical suite on Linux/BusyBox in CI.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = os.environ.get("TSKS_TEST_SHELL", "/bin/sh")
FIRMWARE_TOOLS = ("awk", "cat", "chmod", "cp", "date", "df", "grep", "ln", "ls", "mkdir",
                  "mv", "readlink", "rm", "sha256sum", "sleep", "tail", "tr", "uname", "wc", "which")

MOCK = r'''#!/usr/bin/env python3
import fcntl,json,os,shlex,sys
from pathlib import Path
root=Path(os.environ['MOCK_ROOT']); name=Path(sys.argv[0]).name; args=sys.argv[1:]
def read(n, default):
    try:return json.loads((root/n).read_text())
    except FileNotFoundError:return default
def write(n,data):(root/n).write_text(json.dumps(data))
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps([name,args])+'\n')
if name=='dbus':
    data=read('config.json',{})
    if args[0]=='get':print(data.get(args[1],''))
    elif args[0]=='set':
        key,value=args[1].split('=',1)
        if os.environ.get('REQUIRE_CONFIG_LOCK')=='1':
            with open(Path(os.environ['TSKS_RUN'])/'operation.lock','a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:sys.exit(90)
        if os.environ.get('DBUS_FAIL_ONCE_KEY')==key and not (root/'dbus-failed').exists():
            (root/'dbus-failed').touch();sys.exit(1)
        data[key]=value;write('config.json',data)
    elif args[0]=='remove':data.pop(args[1],None);write('config.json',data)
elif name=='nvram':print(read('nvram.json',{}).get(args[1],''))
elif name=='flock':
    try:fcntl.flock(int(args[-1]),fcntl.LOCK_UN if '-u' in args else fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:sys.exit(1)
elif name=='curl':
    if '-d' in args:(root/'reply.json').write_text(args[args.index('-d')+1])
    else:
        code=read('https.json',0)
        if code==0:print('200')
        sys.exit(code)
elif name=='tsks-helper':
    if args[0]=='quote':print(json.dumps(args[1],ensure_ascii=False))
    elif args[0]=='fifo':os.mkfifo(args[1],0o600)
    elif args[0]=='temp':
        import tempfile
        path=Path(args[1])
        if not path.name.endswith('XXXXXX'):sys.exit(1)
        fd,name=tempfile.mkstemp(prefix=path.name[:-6],dir=path.parent)
        os.close(fd);print(name)
    elif args[0]=='json-get':
        try:
            val=json.loads(Path(args[1]).read_text())
            for part in args[2].split('.'):val=val[int(part)] if isinstance(val,list) else val[part]
            print(val if isinstance(val,str) else json.dumps(val,separators=(',',':')))
        except (FileNotFoundError,KeyError,IndexError,ValueError,TypeError):sys.exit(1)
    elif args[0]=='status':
        queue=read('status-queue.json',[])
        if queue:value=queue.pop(0);write('status-queue.json',queue)
        else:value=read('status.json',{})
        if value is None:sys.exit(1)
        print(json.dumps(value))
    elif args[0]=='timeout':
        import subprocess
        sys.exit(subprocess.run(args[2:]).returncode)
    elif args[0]=='version':print('1.102.4')
    elif args[0]=='log':
        if read('log_exit.json',0):sys.exit(1)
        with open(args[1],'a') as dest:
            for line in sys.stdin:dest.write(line);dest.flush()
elif name in ('tailscale','tailscaled'):
    if name=='tailscaled' and os.environ.get('MOCK_DAEMON')=='1':
        import time
        proc=Path(os.environ['TSKS_PROC'])/str(os.getpid());proc.mkdir(parents=True)
        (proc/'cmdline').write_bytes((sys.argv[0]+'\0'+'\0'.join(args)).encode())
        print('mock daemon started',flush=True)
        while True:time.sleep(1)
    if name=='tailscale':print(read('cli_output.json',''),end='')
    sys.exit(read('cli_exit.json',0))
elif name=='cru':pass
elif name in ('iptables','ip6tables','iptables-save','ip6tables-save'):
    table='filter'
    mode=read('iptables-wait.json','legacy')
    if args==['--help']:
        if mode=='help-failed':sys.exit(1)
        print('iptables: -C, --check chain rule')
        if mode=='wait':print('  -w, --wait  Wait for the xtables lock')
        elif mode=='seconds':print('  --wait -w [seconds]  Wait for the xtables lock')
        sys.exit(0)
    if any(arg.startswith('-w') or arg.startswith('--wait') for arg in args):
        if mode=='legacy':sys.exit(2)
        if args[0]!='-w':sys.exit(2)
        del args[0]
        if args and args[0].isdigit():
            if mode!='seconds':sys.exit(2)
            del args[0]
    if '-t' in args:i=args.index('-t');table=args[i+1];del args[i:i+2]
    db=read('firewall.json',{})
    family=name.split('-')[0]; key=family+':'+table
    chains=db.setdefault(key,{c:[] for c in ['INPUT','OUTPUT','FORWARD','PREROUTING','POSTROUTING']})
    if name.endswith('-save'):
        print('*'+table)
        for chain,rules in chains.items():
            for rule in rules:print('-A '+chain+' '+' '.join(rule))
        print('COMMIT');sys.exit(0)
    op,chain=args[:2];rule=args[2:]
    if read('iptables-fail-once.json','')=='DNAT' and '-j' in rule and rule[rule.index('-j')+1]=='DNAT' and not (root/'iptables-failed').exists():
        (root/'iptables-failed').touch();sys.exit(1)
    if op=='-N':
        if chain in chains:sys.exit(1)
        chains[chain]=[]
    elif chain not in chains:sys.exit(1)
    elif op=='-F':chains[chain]=[]
    elif op=='-X':del chains[chain]
    elif op=='-C':sys.exit(0 if rule in chains[chain] else 1)
    elif op=='-A':chains[chain].append(rule)
    elif op=='-I':chains[chain].insert(int(rule[0])-1,rule[1:])
    elif op=='-D':
        if rule not in chains[chain]:sys.exit(1)
        chains[chain].remove(rule)
    else:raise RuntimeError(args)
    write('firewall.json',db)
else:raise RuntimeError(name)
'''


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tsks-backend-")
        self.base = Path(self.temp.name)
        self.ks = self.base / "koolshare"
        self.run = self.base / "run"
        self.web = self.base / "web"
        self.mock = self.base / "mock"
        self.mock.mkdir()
        self.utilities = self.base / "utilities"
        self.utilities.mkdir()
        busybox = os.environ.get("TSKS_TEST_BUSYBOX")
        for name in FIRMWARE_TOOLS:
            target = busybox or shutil.which(name)
            if not target:
                raise RuntimeError("Test host lacks fixture utility: " + name)
            (self.utilities / name).symlink_to(target)
        (self.ks / "bin").mkdir(parents=True)
        (self.ks / "tailscale/current").mkdir(parents=True)
        shutil.copytree(ROOT / "plugin/scripts", self.ks / "scripts")
        self.command = self.mock / "mock-command"
        self.command.write_text(MOCK.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        self.command.chmod(0o755)
        for name in ("dbus", "nvram", "flock", "curl", "cru", "iptables", "ip6tables", "iptables-save", "ip6tables-save"):
            (self.mock / name).symlink_to(self.command)
        (self.ks / "bin/tsks-helper").symlink_to(self.command)
        for name in ("tailscale", "tailscaled"):
            (self.ks / "tailscale/current" / name).symlink_to(self.command)
        self.env = dict(os.environ, TSKS_ROOT=str(self.ks), TSKS_RUN=str(self.run),
                        TSKS_WEB=str(self.web), TSKS_SYSFS=str(self.base / "sys"),
                        TSKS_PROC=str(self.base / "proc"), MOCK_ROOT=str(self.mock),
                        PATH=os.pathsep.join((str(self.mock), str(self.utilities))))
        self.write("config.json", {"tailscale_enable": "1", "tailscale_watchdog_enable": "1"})
        self.write("nvram.json", {"lan_ifname": "br0", "lan_ipaddr": "192.168.50.1",
                                  "lan_netmask": "255.255.255.0", "wan_primary": "0",
                                  "wan0_state_t": "2", "wan0_link": "1"})
        self.status = dict(ok=True, version="1.102.4", backend_state="Running", online=True,
                           health_codes=[], health_messages=[], auth_url="", ips=["100.64.2.3", "fd7a:115c:a1e0::123"],
                           want_running=True, logged_out=False, sync_enabled=True, monitoring_available=True,
                           node_id="node-original", have_node_key=True)
        self.write("status.json", self.status)
        self.shell(":")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        (self.mock / name).write_text(json.dumps(value))

    def read(self, name):
        return json.loads((self.mock / name).read_text())

    def calls(self, name=None):
        path = self.mock / "calls.jsonl"
        data = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [args for command, args in data if name is None or command == name]

    def shell(self, code, check=True):
        source = '. "$TSKS_ROOT/scripts/tailscale_lib.sh"; ts_init || exit; ' + code
        result = subprocess.run([SHELL, "-c", source], env=self.env, text=True, capture_output=True, timeout=40)
        if check and result.returncode:
            self.fail(f"shell failed ({result.returncode}): {code}\n{result.stdout}\n{result.stderr}")
        return result

    def entry(self, name, *args, check=True):
        result = subprocess.run([SHELL, str(self.ks / "scripts" / name), *args], env=self.env,
                                text=True, capture_output=True, timeout=40)
        if check and result.returncode:
            self.fail(f"{name} failed: {result.stderr}\n{result.stdout}")
        return result

    def tick(self, now, alive=True):
        return self.shell(f"ts_now() {{ echo {now}; }}; ts_pid_alive() {{ return {0 if alive else 1}; }}; "
                          'ts_restart() { echo recovery >>"$RUN/recoveries"; }; ts_lock; ts_watchdog_run')

    def recoveries(self):
        path = self.run / "recoveries"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_bool_validation_never_evaluates_configuration(self):
        payload = "$(touch " + str(self.base / "pwned") + ")"
        self.write("config.json", {"tailscale_enable": payload})
        self.assertNotEqual(self.shell("ts_config_read", check=False).returncode, 0)
        self.assertFalse((self.base / "pwned").exists())
        self.assertEqual(self.calls("dbus")[-1], ["get", "tailscale_enable"])

    def test_busy_nat_event_is_replayed_without_restart_for_unchanged_address(self):
        self.write("config.json", {"tailscale_enable": "1", "tailscale_watchdog_enable": "0"})
        self.tick(20000)
        self.assertTrue((self.run / "firewall-status.json").exists())
        before = len(self.calls("iptables"))
        result = self.shell('ts_lock() { return 1; }; ID=501; ts_mutation start_nat', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.run / "nat-pending").exists())
        self.tick(20060)
        self.assertGreater(len(self.calls("iptables")), before)
        self.assertFalse((self.run / "nat-pending").exists())
        after = len(self.calls("iptables"))
        self.tick(20120)
        self.assertEqual(len(self.calls("iptables")), after)
        self.assertEqual(self.recoveries(), 0)

    def test_lan_cidr_masks_are_validated(self):
        self.assertEqual(self.shell('ts_lan; echo "$LAN_CIDR"').stdout.strip(), "192.168.50.0/24")
        values = self.read("nvram.json")
        for mask in ("255.0.255.0", "0.0.0.0", "255.255.999.0", "255.255.255.0;id"):
            values["lan_netmask"] = mask
            self.write("nvram.json", values)
            self.assertNotEqual(self.shell("ts_lan", check=False).returncode, 0)

    def test_firmware_fixture_excludes_missing_applet_dependencies(self):
        for name in ("od", "mkfifo", "mktemp", "timeout"):
            self.assertNotEqual(self.shell("which " + name, check=False).returncode, 0)

    def test_firewall_legacy_commands_are_bounded_without_wait_option(self):
        self.shell("ts_firewall_apply; ts_firewall_apply")
        for family in ("iptables", "ip6tables"):
            calls = self.calls(family)
            self.assertEqual(calls.count(["--help"]), 1)
            self.assertFalse(any("-w" in args for args in calls))
        firewall_calls = [args for args in self.calls("tsks-helper") if args[0] == "timeout" and args[2].startswith(("iptables", "ip6tables"))]
        self.assertTrue(firewall_calls)
        for args in firewall_calls:
            self.assertEqual(args[1], "3" if args[-1] == "--help" else "5")

    def test_firewall_wait_option_is_advertised_and_bounded(self):
        for mode in ("wait", "seconds"):
            with self.subTest(mode=mode):
                self.write("iptables-wait.json", mode)
                (self.mock / "calls.jsonl").unlink(missing_ok=True)
                self.shell("ts_firewall_apply")
                for family in ("iptables", "ip6tables"):
                    calls = [args for args in self.calls(family) if args != ["--help"]]
                    self.assertTrue(calls)
                    self.assertTrue(all(args[0] == "-w" and args[1] != "2" for args in calls))

    def test_firewall_failed_help_probe_does_not_guess_options_or_mutate(self):
        self.write("iptables-wait.json", "help-failed")
        self.assertNotEqual(self.shell("ts_chain iptables filter INPUT TSKS_INPUT", check=False).returncode, 0)
        self.assertEqual(self.calls("iptables"), [["--help"]])
        self.assertFalse((self.mock / "firewall.json").exists())

    def test_firewall_idempotence_scoping_and_fullcone_preservation(self):
        foreign = ["-m", "comment", "--comment", "tailscale_rule", "-j", "FULLCONENAT"]
        legacy = ["-m", "comment", "--comment", "tailscale_rule", "-j", "MASQUERADE"]
        unrelated = ["-m", "comment", "--comment", "different_tag", "-j", "MASQUERADE"]
        self.write("firewall.json", {"iptables:nat": {"INPUT": [], "OUTPUT": [], "FORWARD": [],
                   "PREROUTING": [], "POSTROUTING": [foreign, legacy, unrelated]}})
        self.write("config.json", {"tailscale_enable": "1", "tailscale_ipv4_enable": "0"})
        self.shell("ts_firewall_apply; ts_firewall_apply")
        fw = self.read("firewall.json")
        self.assertIn(foreign, fw["iptables:nat"]["POSTROUTING"])
        self.assertIn(unrelated, fw["iptables:nat"]["POSTROUTING"])
        self.assertNotIn(legacy, fw["iptables:nat"]["POSTROUTING"])
        self.assertEqual(fw["iptables:filter"]["INPUT"], [["-j", "TSKS_INPUT"]])
        self.assertEqual(fw["iptables:filter"]["TSKS_INPUT"], [["-p", "udp", "--dport", "41641", "-j", "DROP"]])
        self.assertEqual(fw["ip6tables:filter"]["TSKS_INPUT"], [])
        self.assertEqual(fw["iptables:nat"]["TSKS_POSTROUTING"], [["-s", "192.168.50.0/24", "-o", "tailscale0", "-j", "MASQUERADE"]])
        self.assertEqual(fw["iptables:nat"]["TSKS_PREROUTING"], [["-i", "tailscale0", "-d", "100.64.2.3/32", "-j", "DNAT", "--to-destination", "192.168.50.1"]])
        self.shell("ts_firewall_remove; ts_firewall_remove")
        self.assertEqual(self.read("firewall.json")["iptables:nat"]["POSTROUTING"], [foreign, unrelated])

    def test_nat_event_only_reapplies_firewall(self):
        self.entry("tailscale_config", "start_nat")
        self.assertEqual(self.calls("tailscale"), [])
        self.assertEqual(self.calls("tailscaled"), [])
        self.assertEqual(self.calls("cru"), [])

    def test_fullcone_postrouting_hook_stays_ahead_of_plugin_fallback(self):
        hook = ["-j", "FCONE_POST"]
        fullcone_rules = [["-o", "eth0", "-j", "FULLCONENAT"]]
        self.write("firewall.json", {"iptables:nat": {"INPUT": [], "OUTPUT": [], "FORWARD": [],
                   "PREROUTING": [], "POSTROUTING": [["-j", "TSKS_POSTROUTING"], hook],
                   "FCONE_POST": fullcone_rules, "TSKS_POSTROUTING": [["-j", "MASQUERADE"]]}})
        self.shell("ts_firewall_apply; ts_firewall_apply")
        table = self.read("firewall.json")["iptables:nat"]
        self.assertEqual(table["POSTROUTING"], [hook, ["-j", "TSKS_POSTROUTING"]])
        self.assertEqual(table["FCONE_POST"], fullcone_rules)
        self.assertIn("MASQUERADE", table["TSKS_POSTROUTING"][0])

    def test_address_maintenance_after_auth_and_address_change_without_watchdog(self):
        self.write("config.json", {"tailscale_enable": "1", "tailscale_watchdog_enable": "0"})
        self.write("status.json", dict(self.status, backend_state="NeedsLogin", ips=[]))
        self.shell("ts_config_read; ts_cron; ts_firewall_apply")
        self.assertTrue(any(args[0] == "a" for args in self.calls("cru")))
        self.assertEqual(self.read("firewall.json")["iptables:nat"]["TSKS_PREROUTING"], [])
        self.write("status.json", self.status)
        self.tick(20000)
        rule = self.read("firewall.json")["iptables:nat"]["TSKS_PREROUTING"][0]
        self.assertIn("100.64.2.3/32", rule)
        count = len(self.calls("iptables"))
        self.tick(20060)
        self.assertEqual(len(self.calls("iptables")), count)
        self.write("status.json", dict(self.status, ips=["100.64.2.9"]))
        self.tick(20120)
        rules = self.read("firewall.json")["iptables:nat"]["TSKS_PREROUTING"]
        self.assertEqual(len(rules), 1)
        self.assertIn("100.64.2.9/32", rules[0])
        count = len(self.calls("iptables"))
        self.tick(20180)
        self.assertEqual(len(self.calls("iptables")), count)
        self.assertEqual(len([args for args in self.calls("tsks-helper") if args[0] == "status"]), 5)
        self.assertEqual(self.recoveries(), 0)
        self.assertEqual(self.calls("tailscaled"), [])
        self.assertEqual(self.calls("tailscale"), [])

    def test_failed_address_refresh_retries_same_address_without_restart(self):
        self.write("config.json", {"tailscale_enable": "1", "tailscale_watchdog_enable": "0"})
        self.write("status.json", dict(self.status, backend_state="NeedsLogin", ips=[]))
        self.shell("ts_firewall_apply")
        self.write("status.json", self.status)
        self.write("iptables-fail-once.json", "DNAT")
        self.tick(20000)
        self.assertFalse((self.run / "firewall-status.json").exists())
        self.assertEqual(self.read("firewall.json")["iptables:nat"]["TSKS_PREROUTING"], [])
        self.tick(20060)
        self.assertEqual(json.loads((self.run / "firewall-status.json").read_text())["ips"], self.status["ips"])
        self.assertIn("100.64.2.3/32", self.read("firewall.json")["iptables:nat"]["TSKS_PREROUTING"][0])
        count = len(self.calls("iptables"))
        self.tick(20120)
        self.assertEqual(len(self.calls("iptables")), count)
        self.assertEqual(self.recoveries(), 0)

    def test_existing_identity_and_preferences_are_retained_on_start(self):
        state = self.ks / "configs/tailscale/tailscaled.state"
        state.write_bytes(b"secret-persisted-identity\x00prefs")
        self.shell("ts_pid_alive() { return 0; }; ts_start")
        self.assertEqual(state.read_bytes(), b"secret-persisted-identity\x00prefs")
        args = self.calls("tailscale")[0]
        self.assertIn("--netfilter-mode=on", args)
        self.assertNotIn("--accept-dns=false", args)
        self.assertFalse(any("--reset" in a for a in args))
        self.assertEqual(self.calls("tailscaled"), [])
        self.assertIn("* * * * *", self.calls("cru")[-1][-1])

    def test_existing_daemon_start_refreshes_wan_grace_but_nat_does_not(self):
        (self.run / "started-at").write_text("10000")
        self.shell("ts_now() { echo 20000; }; ts_pid_alive() { return 0; }; ts_start")
        self.assertEqual((self.run / "started-at").read_text().strip(), "20000")
        self.entry("tailscale_config", "start_nat")
        self.assertEqual((self.run / "started-at").read_text().strip(), "20000")
        self.assertEqual(self.calls("tailscaled"), [])

    def test_fresh_state_disables_magic_dns_once(self):
        self.shell("ts_pid_alive() { return 0; }; ts_start")
        self.assertIn("--accept-dns=false", self.calls("tailscale")[0])

    def test_daemon_logger_lifecycle_and_children_release_lock(self):
        self.env["MOCK_DAEMON"] = "1"
        try:
            self.shell("ts_lock; ts_start; ts_unlock")
            self.shell("ts_pid_alive; ts_lock; ts_unlock")
            self.assertIn("mock daemon started", (self.run / "daemon.log").read_text())
            self.assertTrue((self.run / "logger.pid").exists())
            self.assertEqual((self.run / "daemon.pipe").stat().st_mode & 0o777, 0o600)
            self.assertIn(["fifo", str(self.run / "daemon.pipe")], self.calls("tsks-helper"))
            pid = (self.run / "tailscaled.pid").read_text()
            (self.run / "tailscaled.pid").unlink()
            self.shell("ts_lock; ts_start; ts_unlock")
            self.assertEqual((self.run / "tailscaled.pid").read_text(), pid)
            self.assertEqual(len(self.calls("tailscaled")), 1)
            self.shell("ts_lock; ts_stop; ts_unlock")
            self.assertFalse((self.run / "daemon.pipe").exists())
            self.assertFalse((self.run / "tailscaled.pid").exists())
        finally:
            pidfile = self.run / "tailscaled.pid"
            if pidfile.exists():
                try:os.kill(int(pidfile.read_text()), 15)
                except ProcessLookupError:pass

    def test_bad_pid_record_does_not_signal_unrelated_process(self):
        (self.run / "tailscaled.pid").write_text(str(os.getpid()))
        proc = self.base / "proc" / str(os.getpid())
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(b"python\0unrelated-process\0")
        self.assertNotEqual(self.shell("ts_pid_alive", check=False).returncode, 0)
        self.shell("ts_stop")

    def test_watchdog_grace_three_failures_and_persistent_budget(self):
        (self.run / "started-at").write_text("10000")
        self.write("status.json", None)
        self.tick(10179, alive=False)
        self.assertFalse((self.run / "watchdog-state").exists())
        for now in (10200, 10260):
            self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 0)
        self.tick(10320, alive=False)
        self.assertEqual(self.recoveries(), 1)
        ledger = self.ks / "configs/tailscale/watchdog-ledger"
        self.assertEqual(ledger.read_text(), "10320\n")
        stamp = ledger.stat().st_mtime_ns
        for now in (10380, 10440, 10500, 10600):
            self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 1)
        self.assertEqual(ledger.stat().st_mtime_ns, stamp)
        self.tick(12120, alive=False)
        self.assertEqual(self.recoveries(), 2)
        # A new shell process and deleted RAM counters do not reset the budget.
        (self.run / "watchdog-state").unlink()
        for now in (20000, 20060, 20120):
            self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 2)
        for now in (97000, 97060, 97120):
            self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 3)

    def test_watchdog_auth_manual_off_and_disabled_exemptions(self):
        (self.run / "started-at").write_text("10000")
        cases = [dict(backend_state="NeedsLogin"), dict(backend_state="NeedsMachineAuth"),
                 dict(backend_state="Stopped"), dict(want_running=False), dict(logged_out=True),
                 dict(sync_enabled=False)]
        for update in cases:
            self.write("status.json", dict(self.status, **update))
            for now in (20000, 20100, 20200):
                self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 0)
        self.write("status.json", None)
        (self.run / "manual-stop").touch()
        for now in (21000, 21100, 21200):
            self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 0)
        (self.run / "manual-stop").unlink()
        self.write("config.json", {"tailscale_enable": "0", "tailscale_watchdog_enable": "1"})
        for now in (22000, 22100, 22200):
            self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 0)

    def test_watchdog_control_timeout_requires_continuity_and_https(self):
        (self.run / "started-at").write_text("10000")
        self.write("status.json", dict(self.status, online=False))
        self.tick(20000)
        self.tick(20599)
        self.assertEqual(self.recoveries(), 0)
        self.write("https.json", 60)
        self.tick(20600)
        self.assertEqual(self.recoveries(), 0)
        self.write("https.json", 0)
        self.tick(20660)
        self.assertEqual(self.recoveries(), 1)
        self.write("status.json", self.status)
        self.tick(24000)
        self.write("status.json", dict(self.status, health_codes=["mapresponse-timeout"]))
        self.tick(24100)
        self.tick(24699)
        self.assertEqual(self.recoveries(), 1)
        self.tick(24700)
        self.assertEqual(self.recoveries(), 2)

    def test_watchdog_clock_rollback_fails_closed(self):
        (self.run / "started-at").write_text("10000")
        (self.ks / "configs/tailscale/watchdog-ledger").write_text("25000\n")
        self.write("status.json", None)
        for now in (20000, 20100, 20200):
            self.tick(now, alive=False)
        self.assertEqual(self.recoveries(), 0)

    def test_watchdog_monitoring_unavailable_never_guesses_control_state(self):
        (self.run / "started-at").write_text("10000")
        self.write("status.json", dict(self.status, monitoring_available=False, online=False))
        for now in (20000, 20600, 22000):
            self.tick(now)
        self.assertEqual(self.recoveries(), 0)
        self.assertEqual(self.calls("curl"), [])

    def test_status_interfaces_and_http_response_json(self):
        self.write("status.json", dict(self.status, health_messages=['safe "quoted" message\nnext line']))
        (self.ks / "tailscale/current/descriptor.json").write_text('{"version":"1.102.4"}')
        (self.ks / "tailscale/available.json").write_text('{"version":"1.104.0"}')
        status = json.loads(self.entry("tailscale_fettle").stdout)
        self.assertEqual(status["core"], {"installed": "1.102.4", "available": "1.104.0", "can_rollback": False})
        self.assertEqual(status["health_messages"], ['safe "quoted" message\nnext line'])
        self.entry("tailscale_fettle", "123456789012345")
        self.assertEqual(self.read("reply.json")["plugin_version"], "3.0.0")
        statistics = self.base / "sys/class/net/tailscale0/statistics"
        statistics.mkdir(parents=True)
        (statistics / "rx_bytes").write_text("12000000000000")
        (statistics / "tx_bytes").write_text("999")
        net = json.loads(self.entry("tailscale_tsnets").stdout)
        self.assertEqual(len(net["interfaces"]), 2)
        self.assertEqual(net["interfaces"][0]["ip"], "100.64.2.3")
        self.assertEqual(net["interfaces"][0]["rx"], 12000000000000)

    def test_mutation_ack_and_atomic_terminal_job(self):
        self.entry("tailscale_config", "000123", "start_nat")
        self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "000123"})
        job = json.loads((self.web / "tailscale3_000123.json").read_text())
        self.assertEqual((job["state"], job["id"]), ("success", "000123"))
        self.assertEqual(list(self.web.glob(".tailscale3*")), [])
        self.assertEqual((self.web / "tailscale3_000123.json").stat().st_mode & 0o777, 0o644)

    def test_connection_details_returns_job_ack_and_bounded_sanitized_log(self):
        self.write("cli_output.json", "100.64.2.3 test-device active\n")
        original = self.read("config.json")
        self.entry("tailscale_status", "000124")
        self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "000124"})
        job = json.loads((self.web / "tailscale3_000124.json").read_text())
        self.assertEqual((job["state"], job["id"]), ("success", "000124"))
        self.assertIn("test-device", (self.web / "tailscale3_000124.log").read_text())
        helper_calls = self.calls("tsks-helper")
        self.assertIn(["temp", str(self.run / "status-result.XXXXXX")], helper_calls)
        self.assertIn(["timeout", "15", str(self.ks / "tailscale/current/tailscale"),
                       "--socket=" + str(self.run / "tailscaled.sock"), "status"], helper_calls)
        self.assertIn(["log", str(self.web / "tailscale3_000124.log"), "65536"], helper_calls)
        self.assertEqual(self.read("config.json"), original)
        self.assertEqual(list(self.run.glob("status-result.*")), [])

    def test_connection_details_cli_or_log_failure_is_terminal_failure(self):
        for failure in ("cli_exit.json", "log_exit.json"):
            with self.subTest(failure=failure):
                self.write("cli_exit.json", 0)
                self.write("log_exit.json", 0)
                self.write(failure, 1)
                result = self.entry("tailscale_status", "125", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "125"})
                job = json.loads((self.web / "tailscale3_125.json").read_text())
                self.assertEqual(job["state"], "failed")
                self.assertEqual(list(self.run.glob("status-result.*")), [])

    def test_shared_lock_rejects_parallel_mutation(self):
        source = '. "$TSKS_ROOT/scripts/tailscale_lib.sh"; ts_init; ts_lock; echo locked; read done'
        proc = subprocess.Popen([SHELL, "-c", source], env=self.env, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            result = self.entry("tailscale_config", "88", "start_nat", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.read("reply.json"), {"accepted": False, "error": "busy"})
            self.assertFalse((self.web / "tailscale3_88.json").exists())
        finally:
            proc.communicate("done\n", timeout=5)

    def test_logs_are_bounded_and_public_jobs_retained(self):
        self.shell('ts_job_begin 7; i=0; while [ "$i" -lt 100 ]; do ts_job_log "$(printf "%0500d" 0)"; i=$((i+1)); done')
        self.assertLessEqual((self.run / "events.log").stat().st_size, 32768)
        self.assertLessEqual((self.web / "tailscale3_7.log").stat().st_size, 32768)

    def test_seven_bit_snapshot_is_applied_under_lock_in_documented_order(self):
        self.env["REQUIRE_CONFIG_LOCK"] = "1"
        self.entry("tailscale_config", "200", "web_submit", "0101010")
        keys = ["tailscale_enable", "tailscale_ipv4_enable", "tailscale_ipv6_enable", "tailscale_advertise_routes",
                "tailscale_accept_routes", "tailscale_exit_node", "tailscale_watchdog_enable"]
        self.assertEqual(self.read("config.json"), dict(zip(keys, "0101010")))
        self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "200"})
        self.assertEqual(json.loads((self.web / "tailscale3_200.json").read_text())["state"], "success")

    def test_invalid_snapshots_never_write_dbus(self):
        original = self.read("config.json")
        for bits in ("", "101010", "10101010", "10101x1", "$(touch /tmp/invalid)"):
            with self.subTest(bits=bits):
                self.assertNotEqual(self.entry("tailscale_config", "201", "web_submit", bits, check=False).returncode, 0)
                self.assertEqual(self.read("reply.json"), {"accepted": False, "error": "invalid_config_snapshot"})
                self.assertEqual(self.read("config.json"), original)
        self.assertEqual([args for args in self.calls("dbus") if args[0] != "get"], [])

    def test_busy_snapshot_never_changes_saved_settings(self):
        original = self.read("config.json")
        source = '. "$TSKS_ROOT/scripts/tailscale_lib.sh"; ts_init; ts_lock; echo locked; read done'
        proc = subprocess.Popen([SHELL, "-c", source], env=self.env, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            self.assertNotEqual(self.entry("tailscale_config", "202", "web_submit", "0000000", check=False).returncode, 0)
            self.assertEqual(self.read("reply.json"), {"accepted": False, "error": "busy"})
            self.assertEqual(self.read("config.json"), original)
            self.assertEqual([args for args in self.calls("dbus") if args[0] != "get"], [])
        finally:
            proc.communicate("done\n", timeout=5)

    def test_partial_dbus_write_failure_restores_old_values_and_absent_keys(self):
        original = self.read("config.json")
        self.env["DBUS_FAIL_ONCE_KEY"] = "tailscale_accept_routes"
        self.env["REQUIRE_CONFIG_LOCK"] = "1"
        result = self.entry("tailscale_config", "203", "web_submit", "0000000", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read("config.json"), original)
        self.assertEqual(self.calls("tailscale"), [])
        self.assertEqual(json.loads((self.web / "tailscale3_203.json").read_text())["state"], "failed")

    def test_failed_service_action_restores_settings_and_previous_running_state(self):
        original = self.read("config.json")
        code = r'''
            ts_pid_alive() { return 0; }
            ts_restart() {
                value=$(dbus get tailscale_ipv4_enable)
                printf '%s\n' "restart:$value" >>"$RUN/config-lifecycle"
                [ "$value" != 0 ]
            }
            ID=204; ts_mutation web_submit 1000000
        '''
        result = self.shell(code, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read("config.json"), original)
        self.assertEqual((self.run / "config-lifecycle").read_text().splitlines(), ["restart:0", "restart:"])
        self.assertEqual(json.loads((self.web / "tailscale3_204.json").read_text())["state"], "failed")

    def test_failed_service_action_restores_manual_stop(self):
        original = self.read("config.json")
        (self.run / "manual-stop").touch()
        code = r'''
            ts_pid_alive() { return 1; }
            ts_restart() { rm -f "$RUN/manual-stop"; return 1; }
            ts_stop() { echo stopped >"$RUN/config-stopped"; }
            ID=205; ts_mutation web_submit 1000000
        '''
        self.assertNotEqual(self.shell(code, check=False).returncode, 0)
        self.assertEqual(self.read("config.json"), original)
        self.assertTrue((self.run / "manual-stop").exists())
        self.assertTrue((self.run / "config-stopped").exists())

    def test_watchdog_fresh_sample_cancels_recovery_without_spending_budget(self):
        (self.run / "started-at").write_text("10000")
        self.write("status.json", None)
        self.tick(20000)
        self.tick(20060)
        self.write("status-queue.json", [None, self.status])
        self.tick(20120)
        self.assertEqual(self.recoveries(), 0)
        self.assertFalse((self.ks / "configs/tailscale/watchdog-ledger").exists())
        # The control connection recovers between the threshold sample and the
        # final sample: no HTTPS probe or daemon restart is necessary.
        (self.run / "watchdog-state").write_text("failures=0\ncontrol_since=20000\n")
        self.write("status-queue.json", [dict(self.status, online=False), self.status])
        self.tick(20600)
        self.assertEqual(self.recoveries(), 0)
        self.assertEqual(self.calls("curl"), [])

    def test_job_files_refuse_preexisting_symlinks_and_predictable_temps(self):
        self.web.mkdir()
        victim = self.base / "do-not-touch"
        victim.write_text("private-content")
        for suffix in ("log", "json"):
            link = self.web / f"tailscale3_300.{suffix}"
            link.symlink_to(victim)
            self.assertNotEqual(self.shell("ts_job_begin 300", check=False).returncode, 0)
            self.assertEqual(victim.read_text(), "private-content")
            link.unlink()
        self.shell('ln -s "$MOCK_ROOT/../do-not-touch" "$WEB/.tailscale3_301.$$"; TS_JOB=301; ts_job_write success complete done')
        self.assertEqual(victim.read_text(), "private-content")
        self.assertEqual(json.loads((self.web / "tailscale3_301.json").read_text())["state"], "success")
        (self.web / "tailscale3_301.log").symlink_to(victim)
        self.assertNotEqual(self.shell('TS_JOB=301; ts_job_log hello', check=False).returncode, 0)
        self.assertEqual(victim.read_text(), "private-content")


if __name__ == "__main__":
    unittest.main()
