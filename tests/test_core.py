"""Core transaction fault injection on isolated files and mock commands.

Cryptographic/ELF/archive validation is tested by the Go helper tests. Here the
helper reports deterministic failures so transaction ordering, lifecycle locks,
state preservation and recovery can be checked without networking or routers.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import test_backend as backend


CORE_COMMANDS = r'''
    if args[0]=='fetch':
        kind='manifest' if args[1].endswith('manifest.json') else 'archive'
        if read('fault.json','')=='fetch-'+kind:sys.exit(1)
        Path(args[2]).write_text(json.dumps(read('feed.json',{})))
    elif args[0]=='verify':
        if read('fault.json','')=='signature':sys.exit(1)
        print(json.dumps(read('feed.json',{})))
    elif args[0]=='extract':
        import hashlib
        if read('fault.json','')=='archive-hash':sys.exit(1)
        desc=json.loads(Path(args[2]).read_text());dest=Path(args[3]);dest.mkdir(parents=True)
        data=('version:'+desc['version']+'\n'+desc['source_commit']+'\n').encode()
        if read('fault.json','')=='binary-hash':data+=b'corruption'
        (dest/'tailscale.combined').write_bytes(data);(dest/'tailscale.combined').chmod(0o755)
        (dest/'descriptor.json').write_text(json.dumps(desc))
        for name in ('tailscale','tailscaled'):(dest/name).symlink_to('tailscale.combined')
    elif args[0]=='atomic-link':
        if read('fault.json','')=='switch-link' and args[1]==read('feed-target.json',''):sys.exit(1)
        path=Path(args[2]);temp=path.with_name('.link-'+str(os.getpid()))
        temp.symlink_to(args[1]);temp.replace(path)
    elif args[0]=='compare':
        a,b=[tuple(map(int,x.split('.'))) for x in args[1:]]
        print((a>b)-(a<b))
    elif args[0]=='quote':
'''

EXTRA_COMMANDS = r'''
elif name=='df':
    value=read('space.json',102400)
    if isinstance(value,list):
        available=value[0]
        if len(value)>1:write('space.json',value[1:])
    else:available=value
    print('Filesystem 1024-blocks Used Available Capacity Mounted on')
    print('mock 200000 10000 '+str(available)+' 5% /isolated')
elif name=='sync':pass
elif name=='cp':
    import subprocess
    if read('fault.json','')=='persistent-copy' and '/.candidate-' in args[-1]:
        dest=Path(args[-1]);dest.mkdir(parents=True,exist_ok=True)
        (dest/'partial-copy').write_text('incomplete');sys.exit(1)
    sys.exit(subprocess.run(['/bin/cp',*args]).returncode)
'''

LIFECYCLE = r'''
ts_stop() {
    printf 'stop %s\n' "$(readlink "$DATA/current")" >>"$RUN/lifecycle"
    [ ! -f "$MOCK_ROOT/fail-stop" ]
}
ts_start() {
    local target
    target=$(readlink "$DATA/current")
    printf 'start %s\n' "$target" >>"$RUN/lifecycle"
    if [ -f "$MOCK_ROOT/fail-start-target" ] && [ "$(cat "$MOCK_ROOT/fail-start-target")" = "$target" ]; then
        printf 'candidate-mutated-identity' >"$STATE"
        return 1
    fi
    return 0
}
'''


class CoreTests(unittest.TestCase):
    write = backend.BackendTests.write
    read = backend.BackendTests.read
    calls = backend.BackendTests.calls
    entry = backend.BackendTests.entry
    tearDown = backend.BackendTests.tearDown

    def setUp(self):
        backend.BackendTests.setUp(self)
        script = backend.MOCK.replace("    if args[0]=='quote':", CORE_COMMANDS.rstrip())
        script = script.replace("elif name=='cru':pass", "elif name=='cru':pass" + EXTRA_COMMANDS)
        script = script.replace("elif args[0]=='version':print('1.102.4')", "elif args[0]=='version':print(Path(args[1]).read_text().splitlines()[0].split(':',1)[1])")
        script = script.replace("print(json.dumps(value))", "current=Path(os.environ['TSKS_ROOT'])/'tailscale/current';value.update(read('status-by-core.json',{}).get(os.readlink(current),{}));value['version']=json.loads((current/'descriptor.json').read_text())['version'];print(json.dumps(value))")
        self.command.write_text(script.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        for name in ("df", "sync", "cp"):
            (self.mock / name).symlink_to(self.command)
        self.data = self.ks / "tailscale"
        shutil.rmtree(self.data / "current")
        self.old = self.make_core("1.102.4", "r2", "a" * 40)
        (self.data / "current").symlink_to(self.old)
        (self.data / "arch").write_text("arm\n")
        (self.data / "release.pub").write_text("test-public-key\n")
        (self.base / "proc").mkdir(exist_ok=True)
        (self.base / "proc/meminfo").write_text("MemAvailable: 100000 kB\nMemFree: 100000 kB\n")
        self.state = self.ks / "configs/tailscale/tailscaled.state"
        self.state.write_text("original-identity-and-current-preferences")
        self.newdesc = self.descriptor("1.104.0", "r1", "b" * 40)
        self.offer(self.newdesc)

    @staticmethod
    def descriptor(version, build, commit):
        binary = f"version:{version}\n{commit}\n".encode()
        return dict(version=version, build=build, arch="arm", source_commit=commit,
                    recipe_sha256="c" * 64, binary_sha256=hashlib.sha256(binary).hexdigest(),
                    unpacked_size=len(binary), size=1024, sha256="d" * 64,
                    url=f"https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v{version}-{build}/tailscale-core_{version}_{build}_arm.tar.gz")

    def make_core(self, version, build, commit):
        descriptor = self.descriptor(version, build, commit)
        target = f"cores/{version}-{build}-arm"
        folder = self.data / target
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "descriptor.json").write_text(json.dumps(descriptor))
        (folder / "tailscale.combined").write_text(f"version:{version}\n{commit}\n")
        (folder / "tailscale.combined").chmod(0o755)
        for name in ("tailscale", "tailscaled"):
            link = folder / name
            if not link.exists():
                link.symlink_to("tailscale.combined")
        return target

    def offer(self, descriptor):
        self.write("feed.json", descriptor)
        self.new = f"cores/{descriptor['version']}-{descriptor['build']}-{descriptor['arch']}"
        self.write("feed-target.json", self.new)

    def shell(self, code, check=True):
        # Backend fixture calls this before core fixtures exist; sourcing has no
        # effects and lets all subsequent calls use the same shared lock.
        return backend.BackendTests.shell(self, '. "$TSKS_ROOT/scripts/tailscale_core_lib.sh"; ' + LIFECYCLE + "\n" + code, check=check)

    def lifecycle(self):
        path = self.run / "lifecycle"
        return path.read_text().splitlines() if path.exists() else []

    def assert_untouched(self):
        self.assertEqual(os.readlink(self.data / "current"), self.old)
        self.assertEqual(self.state.read_text(), "original-identity-and-current-preferences")
        self.assertEqual(self.lifecycle(), [])
        self.assertFalse((self.data / "update.txn").exists())

    def journal(self, phase, had_state="1", enabled="1"):
        self.make_core(self.newdesc["version"], self.newdesc["build"], self.newdesc["source_commit"])
        journal = dict(phase=phase, old=self.old, new=self.new, had_state=had_state, enabled=enabled)
        (self.data / "update.txn").write_text(json.dumps(journal))
        if phase in ("switched", "committed"):
            (self.data / "current").unlink()
            (self.data / "current").symlink_to(self.new)
        if had_state == "1" and phase != "prepared":
            (self.data / "update.state").write_text("snapshot-identity-before-update")
        self.state.write_text("identity-at-interruption")

    def test_verified_update_commits_and_preserves_identity(self):
        self.shell("ts_lock; ts_job_begin 101; ts_core_update")
        self.assertEqual(os.readlink(self.data / "current"), self.new)
        self.assertEqual(os.readlink(self.data / "previous"), self.old)
        self.assertEqual(self.state.read_text(), "original-identity-and-current-preferences")
        self.assertEqual(self.lifecycle(), [f"stop {self.old}", f"start {self.new}"])
        self.assertFalse((self.data / "update.txn").exists())
        self.assertFalse((self.data / "update.state").exists())

    def test_download_signature_and_hash_failures_never_stop_current(self):
        for fault in ("fetch-manifest", "signature", "fetch-archive", "archive-hash", "binary-hash"):
            with self.subTest(fault=fault):
                self.write("fault.json", fault)
                result = self.shell("ts_lock; ts_job_begin 102; ts_core_update", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assert_untouched()

    def test_feed_version_and_build_downgrades_are_rejected(self):
        for version, build in (("1.100.0", "r9"), ("1.102.4", "r1")):
            with self.subTest(version=version, build=build):
                self.offer(self.descriptor(version, build, "b" * 40))
                self.assertNotEqual(self.shell("ts_lock; ts_core_update", check=False).returncode, 0)
                self.assert_untouched()

    def test_same_target_requires_matching_metadata_and_binary_hash(self):
        offered = self.descriptor("1.102.4", "r2", "b" * 40)
        self.offer(offered)
        self.assertNotEqual(self.shell("ts_lock; ts_core_update", check=False).returncode, 0)
        self.assert_untouched()
        self.offer(self.descriptor("1.102.4", "r2", "a" * 40))
        (self.data / self.old / "tailscale.combined").write_text("version:1.102.4\ncorrupted")
        self.assertNotEqual(self.shell("ts_lock; ts_core_update", check=False).returncode, 0)
        self.assertEqual(self.lifecycle(), [])

    def test_low_memory_or_storage_rejects_before_daemon_stop(self):
        (self.base / "proc/meminfo").write_text("MemAvailable: 1000 kB\n")
        self.assertNotEqual(self.shell("ts_lock; ts_core_update", check=False).returncode, 0)
        self.assert_untouched()
        (self.base / "proc/meminfo").write_text("MemAvailable: 100000 kB\n")
        self.write("space.json", 8192)
        self.assertNotEqual(self.shell("ts_lock; ts_core_update", check=False).returncode, 0)
        self.assert_untouched()
        oversized = dict(self.newdesc, unpacked_size=12582913)
        self.offer(oversized)
        self.write("space.json", 999999)
        self.assertNotEqual(self.shell("ts_lock; ts_core_update", check=False).returncode, 0)
        self.assert_untouched()

    def test_storage_cleanup_only_removes_previous_never_current(self):
        previous = self.make_core("1.100.0", "r1", "e" * 40)
        (self.data / "previous").symlink_to(previous)
        self.write("space.json", [8000, 100000])
        self.shell("ts_lock; ts_core_update")
        self.assertFalse((self.data / previous).exists())
        self.assertTrue((self.data / self.old / "tailscale.combined").exists())
        self.assertEqual(os.readlink(self.data / "previous"), self.old)

    def test_stale_candidate_and_copy_failure_are_cleaned(self):
        candidate = self.data / "cores/.candidate-103"
        candidate.mkdir()
        (candidate / "stale-file").write_text("partial earlier update")
        self.write("fault.json", "persistent-copy")
        self.assertNotEqual(self.shell("ts_lock; ts_job_begin 103; ts_core_update", check=False).returncode, 0)
        self.assertFalse(candidate.exists())
        self.assert_untouched()
        self.write("fault.json", "")
        candidate.mkdir()
        (candidate / "stale-file").write_text("partial earlier update")
        self.shell("ts_lock; ts_job_begin 103; ts_core_update")
        self.assertFalse(candidate.exists())
        self.assertFalse((self.data / self.new / "stale-file").exists())
        self.assertFalse((self.data / self.new / "core").exists())

    def test_startup_failure_rolls_back_core_and_state(self):
        (self.mock / "fail-start-target").write_text(self.new)
        result = self.shell("ts_lock; ts_job_begin 104; ts_core_update", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(os.readlink(self.data / "current"), self.old)
        self.assertEqual(self.state.read_text(), "original-identity-and-current-preferences")
        self.assertEqual(self.lifecycle(), [f"stop {self.old}", f"start {self.new}", f"stop {self.new}", f"start {self.old}"])
        job = json.loads((self.web / "tailscale3_104.json").read_text())
        self.assertEqual(job["state"], "rolled_back")
        self.assertFalse((self.data / "update.txn").exists())

    def test_changed_node_identity_rolls_back_before_commit(self):
        self.write("status-by-core.json", {self.new: {"node_id": "different-node"}})
        result = self.shell("ts_lock; ts_job_begin 114; ts_core_update", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(os.readlink(self.data / "current"), self.old)
        self.assertEqual(self.state.read_text(), "original-identity-and-current-preferences")
        self.assertEqual(json.loads((self.web / "tailscale3_114.json").read_text())["state"], "rolled_back")

    def test_unexpected_reauthentication_rolls_back_before_commit(self):
        self.write("status-by-core.json", {self.new: {"backend_state": "NeedsLogin", "have_node_key": False}})
        result = self.shell("ts_lock; ts_job_begin 115; ts_core_update", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(os.readlink(self.data / "current"), self.old)
        self.assertEqual(self.state.read_text(), "original-identity-and-current-preferences")
        self.assertEqual(json.loads((self.web / "tailscale3_115.json").read_text())["state"], "rolled_back")

    def test_atomic_pointer_failure_recovers_without_state_loss(self):
        self.write("fault.json", "switch-link")
        self.assertNotEqual(self.shell("ts_lock; ts_core_update", check=False).returncode, 0)
        self.assertEqual(os.readlink(self.data / "current"), self.old)
        self.assertEqual(self.state.read_text(), "original-identity-and-current-preferences")
        self.assertFalse((self.data / "update.txn").exists())

    def test_disabled_plugin_update_does_not_start_daemon(self):
        self.write("config.json", {"tailscale_enable": "0"})
        self.shell("ts_lock; ts_core_update")
        self.assertEqual(os.readlink(self.data / "current"), self.new)
        self.assertEqual(self.lifecycle(), [f"stop {self.old}"])
        self.assertEqual(self.state.read_text(), "original-identity-and-current-preferences")

    def test_manual_rollback_uses_current_state_not_an_old_snapshot(self):
        self.shell("ts_lock; ts_core_update")
        self.state.write_text("recent-device-identity-and-preferences")
        (self.data / "update.state").write_text("unrelated-old-backup")
        self.shell("ts_lock; ts_core_rollback")
        self.assertEqual(os.readlink(self.data / "current"), self.old)
        self.assertEqual(os.readlink(self.data / "previous"), self.new)
        self.assertEqual(self.state.read_text(), "recent-device-identity-and-preferences")
        self.assertFalse((self.data / "update.state").exists())

    def test_interrupted_prepared_phase_does_not_restore_uncommitted_backup(self):
        self.journal("prepared")
        (self.data / "update.state").write_text("stale-backup-must-not-be-used")
        self.shell("ts_lock; ts_core_recover")
        self.assertEqual(os.readlink(self.data / "current"), self.old)
        self.assertEqual(self.state.read_text(), "identity-at-interruption")
        self.assertFalse((self.data / "update.txn").exists())

    def test_interrupted_backed_up_and_switched_restore_snapshot(self):
        for phase in ("backed_up", "switched"):
            with self.subTest(phase=phase):
                self.journal(phase)
                self.shell("ts_lock; ts_core_recover")
                self.assertEqual(os.readlink(self.data / "current"), self.old)
                self.assertEqual(self.state.read_text(), "snapshot-identity-before-update")
                self.assertFalse((self.data / "update.txn").exists())

    def test_interrupted_committed_finishes_without_undoing_new_identity(self):
        self.journal("committed")
        self.shell("ts_lock; ts_core_recover")
        self.assertEqual(os.readlink(self.data / "current"), self.new)
        self.assertEqual(os.readlink(self.data / "previous"), self.old)
        self.assertEqual(self.state.read_text(), "identity-at-interruption")
        self.assertEqual(self.lifecycle(), [])
        self.assertFalse((self.data / "update.txn").exists())

    def test_rollback_preserves_identity_created_when_no_original_state_existed(self):
        self.journal("switched", had_state="0")
        self.shell("ts_lock; ts_core_recover")
        self.assertEqual(self.state.read_text(), "identity-at-interruption")
        self.assertEqual(os.readlink(self.data / "current"), self.old)

    def test_invalid_journal_and_missing_backup_fail_closed(self):
        self.journal("switched")
        (self.data / "update.state").unlink()
        self.assertNotEqual(self.shell("ts_lock; ts_core_recover", check=False).returncode, 0)
        self.assertTrue((self.data / "update.txn").exists())
        self.assertEqual(self.state.read_text(), "identity-at-interruption")
        (self.data / "update.txn").write_text('{"old":"../../outside","new":"cores/test"}')
        self.assertNotEqual(self.shell("ts_lock; ts_core_recover", check=False).returncode, 0)

    def test_config_recovers_before_action_and_watchdog_skips_pending_transaction(self):
        self.journal("switched")
        self.shell('ID=105; ts_mutation stop')
        self.assertEqual(self.lifecycle(), [f"stop {self.new}", f"start {self.old}", f"stop {self.old}"])
        self.assertEqual(self.state.read_text(), "snapshot-identity-before-update")
        self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "105"})
        self.journal("switched")
        self.shell('ts_lock; ts_watchdog_run')
        self.assertTrue((self.data / "update.txn").exists())
        self.assertFalse((self.run / "watchdog-state").exists())

    def test_bad_journal_is_acknowledged_and_reported_as_failed_job(self):
        (self.data / "update.txn").write_text("invalid JSON")
        result = self.shell('ID=106; ts_mutation stop', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "106"})
        job = json.loads((self.web / "tailscale3_106.json").read_text())
        self.assertEqual((job["state"], job["phase"]), ("failed", "recovery"))
        self.assertEqual(self.lifecycle(), [])

    def test_shared_lock_excludes_stop_and_updater_in_both_directions(self):
        source = '. "$TSKS_ROOT/scripts/tailscale_lib.sh"; ts_init; ts_lock; echo locked; read done'
        for method, action in (("tailscale_core", "update"), ("tailscale_config", "stop")):
            with self.subTest(method=method):
                proc = subprocess.Popen([backend.SHELL, "-c", source], env=self.env, text=True,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.assertEqual(proc.stdout.readline().strip(), "locked")
                    result = self.entry(method, "107", action, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.read("reply.json"), {"accepted": False, "error": "busy"})
                    self.assert_untouched()
                finally:
                    proc.communicate("done\n", timeout=5)

    def test_public_check_job_lookup_and_unknown_job_shapes(self):
        self.entry("tailscale_core", "108", "check")
        self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "108"})
        self.entry("tailscale_job", "109", "108")
        self.assertEqual(self.read("reply.json")["state"], "success")
        self.entry("tailscale_job", "110", "9999999")
        self.assertEqual(self.read("reply.json")["state"], "unknown")
        self.assertNotEqual(self.entry("tailscale_job", "111", "../../outside", check=False).returncode, 0)
        self.assert_untouched()

    def test_public_core_recovery_failure_has_accepted_ack_and_terminal_job(self):
        (self.data / "update.txn").write_text("invalid JSON")
        result = self.entry("tailscale_core", "112", "check", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read("reply.json"), {"accepted": True, "job_id": "112"})
        job = json.loads((self.web / "tailscale3_112.json").read_text())
        self.assertEqual((job["state"], job["phase"]), ("failed", "recovery_required"))

    def test_diagnostic_summary_excludes_identity_and_authorization_url(self):
        self.write("status.json", dict(self.status, auth_url="https://login.tailscale.com/a/private-auth-token"))
        (self.base / "proc/uptime").write_text("12345.01 123.0\n")
        self.entry("tailscale_diagnostics", "113")
        job = json.loads((self.web / "tailscale3_113.json").read_text())
        self.assertEqual(job["state"], "success")
        for path in self.web.iterdir():
            content = path.read_text()
            self.assertNotIn("private-auth-token", content)
            self.assertNotIn("original-identity-and-current-preferences", content)
        self.assertTrue((self.run / "diagnostics.json").exists())


if __name__ == "__main__":
    unittest.main()
