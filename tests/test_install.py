"""Transactional installer simulations; all commands and roots are local fixtures.

The helper is a fixture here to isolate installation/rollback behavior. Actual
signature, archive and timeout security is tested by cmd/tsks-helper Go tests.
The installer gets an explicit command inventory, never the host's full PATH.
The BusyBox runner supplies the same-version applets for that inventory.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = os.environ.get('TSKS_TEST_SHELL', '/bin/sh')
FIRMWARE_TOOLS = ('awk', 'cat', 'chmod', 'cp', 'dirname', 'find', 'grep', 'ln',
                  'mkdir', 'readlink', 'rm', 'rmdir', 'sed', 'sleep',
                  'sync', 'tr', 'wc', 'which')
MOCK = r'''#!/usr/bin/env python3
import fcntl,hashlib,json,os,sys
from pathlib import Path
root=Path(os.environ['MOCK_ROOT']); name=Path(sys.argv[0]).name; args=sys.argv[1:]
def read(name,default):
    try:return json.loads((root/name).read_text())
    except FileNotFoundError:return default
def write(name,data):(root/name).write_text(json.dumps(data))
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps([name,args])+'\n')
if name=='nvram':print(read('nvram.json',{}).get(args[1],''))
elif name=='uname':print(read('kernel.json','4.19.183'))
elif name=='df':print('Filesystem 1024-blocks Used Available Capacity Mounted\nfixture 1048576 0 '+str(read('space.json',1048576))+' 0% /')
elif name=='mv':
    import subprocess
    if (root/'fail-move').exists() and args[-1].endswith('/res/tailscale3.js'):
        (root/'fail-move').unlink();sys.exit(1)
    sys.exit(subprocess.run(['/bin/mv',*args]).returncode)
elif name=='dbus':
    data=read('config.json',{})
    if args[0]=='get':print(data.get(args[1],''))
    elif args[0]=='set':
        key,value=args[1].split('=',1);data[key]=value;write('config.json',data)
    elif args[0]=='remove':data.pop(args[1],None);write('config.json',data)
elif name=='flock':
    try:fcntl.flock(int(args[-1]),fcntl.LOCK_UN if '-u' in args else fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:sys.exit(1)
elif name=='openssl':
    if args[:2]!=['dgst','-sha256'] or len(args)!=3:sys.exit(1)
    print('SHA256('+args[2]+')= '+hashlib.sha256(Path(args[2]).read_bytes()).hexdigest())
elif name=='sha256sum':print(hashlib.sha256(Path(args[0]).read_bytes()).hexdigest()+'  '+args[0])
elif name=='tsks-helper':
    if args[0]=='sha256':print(hashlib.sha256(Path(args[1]).read_bytes()).hexdigest())
    elif args[0]=='check-tree':
        base=Path(args[1]);seen=set()
        try:
            for line in (base/'manifest.sha256').read_text().splitlines():
                digest,name=line.split('  ',1)
                if name in seen:sys.exit(1)
                seen.add(name)
                file=base/name
                if file.is_symlink() or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=digest:sys.exit(1)
            if {p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file() and p.name!='manifest.sha256'}!=seen:sys.exit(1)
        except (OSError,ValueError):sys.exit(1)
    elif args[0]=='elf':
        data=Path(args[1]).read_bytes()
        expected={'arm':(1,40),'arm64':(2,183)}.get(args[2])
        if not expected or len(data)<20:sys.exit(1)
        if data[:6]!=b'\x7fELF'+bytes((expected[0],1)) or int.from_bytes(data[18:20],'little')!=expected[1]:sys.exit(1)
    elif args[0]=='verify':
        data=json.loads(Path(args[1]).read_text())
        if not data.get('valid'):sys.exit(1)
        print(json.dumps(data[args[3]]))
    elif args[0]=='json-get':
        try:
            data=json.loads(Path(args[1]).read_text())
            for key in args[2].split('.'):data=data[key]
            print(data if isinstance(data,str) else json.dumps(data))
        except (FileNotFoundError,KeyError,ValueError):sys.exit(1)
    elif args[0]=='status':print(json.dumps({'ok':True}))
    elif args[0]=='version':
        digest=hashlib.sha256(Path(args[1]).read_bytes()).hexdigest()
        value=read('versions.json',{}).get(digest)
        if not value:sys.exit(1)
        print(value)
    elif args[0]=='atomic-link':
        target=Path(args[2]); temporary=target.with_name(target.name+'.new')
        temporary.symlink_to(args[1]);temporary.replace(target)
    else:raise RuntimeError(args)
else:raise RuntimeError(name)
'''
LIBRARY = r'''#!/bin/sh
ts_init() {
 KSROOT=$TSKS_ROOT; DATA=$KSROOT/tailscale; RUN=$TSKS_RUN
 STATE=$KSROOT/configs/tailscale/tailscaled.state
 mkdir -p "$RUN" "${STATE%/*}"
 TS_LOCKED=0
}
ts_lock() { exec 9>"$RUN/operation.lock"; flock -n 9 || return; TS_LOCKED=1; }
ts_unlock() { flock -u 9; exec 9>&-; }
ts_stop() { printf '%s\n' stop >>"$MOCK_ROOT/lifecycle"; }
ts_start() {
 printf 'start version=%s\n' "$(dbus get tailscale_version)" >>"$MOCK_ROOT/lifecycle"
 if [ -f "$MOCK_ROOT/fail-start" ]; then
   rm -f "$MOCK_ROOT/fail-start"
   printf damaged >"$STATE"
   return 1
 fi
 return 0
}
ts_firewall_apply() { printf '%s\n' firewall >>"$MOCK_ROOT/lifecycle"; }
'''


def binary(arch, marker=b'new'):
    data = bytearray(128)
    data[:6] = b'\x7fELF' + bytes([1 if arch == 'arm' else 2, 1])
    data[18:20] = (40 if arch == 'arm' else 183).to_bytes(2, 'little')
    data[64:64+len(marker)] = marker
    return bytes(data)


def descriptor(content, arch, version='1.102.4', build='r1'):
    return dict(schema=1, arch=arch, version=version, build=build, unpacked_size=len(content),
                binary_sha256=hashlib.sha256(content).hexdigest())


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tsks-install-')
        self.base = Path(self.temp.name)
        self.pkg = self.base / 'package/tailscale'
        self.ks = self.base / 'koolshare'
        self.mock = self.base / 'mock'
        self.utilities = self.base / 'utilities'
        self.run = self.base / 'run'
        self.ks.mkdir(); self.mock.mkdir(); self.utilities.mkdir(); self.run.mkdir()
        busybox = os.environ.get('TSKS_TEST_BUSYBOX')
        for name in FIRMWARE_TOOLS:
            target = busybox or shutil.which(name)
            if not target:
                raise RuntimeError('Test host lacks fixture utility: ' + name)
            (self.utilities / name).symlink_to(target)
        shutil.copytree(ROOT / 'plugin', self.pkg)
        (self.pkg / 'version').write_text('3.0.0\n')
        (self.pkg / '.valid').write_text('hnd\nqca\nipq32\nipq64\nmtk\n')
        (self.pkg / 'scripts/tailscale_lib.sh').write_text(LIBRARY)
        command = self.mock / 'mock-command'
        command.write_text(MOCK.replace('#!/usr/bin/env python3', '#!' + sys.executable))
        command.chmod(0o755)
        for name in ('dbus', 'nvram', 'uname', 'flock', 'df', 'mv'):
            (self.mock / name).symlink_to(command)
        (self.mock / 'openssl').symlink_to(os.environ.get('TSKS_TEST_OPENSSL') or command)
        manifest = {'valid': True}; versions = {}
        for arch in ('arm', 'arm64'):
            target = self.pkg / 'payload' / arch
            target.mkdir(parents=True)
            core = binary(arch)
            (target / 'tailscale.combined').write_bytes(core)
            (target / 'tailscale.combined').chmod(0o755)
            shutil.copyfile(command, target / 'tsks-helper')
            (target / 'tsks-helper').chmod(0o755)
            desc = descriptor(core, arch)
            (target / 'descriptor.json').write_text(json.dumps(desc))
            manifest[arch] = desc
            versions[desc['binary_sha256']] = desc['version']
        (self.pkg / 'release.json').write_text(json.dumps(manifest))
        self.write('versions.json', versions)
        self.write('config.json', {})
        self.write('nvram.json', {'productid': 'RT-AX88U'})
        self.env = dict(os.environ, TSKS_ROOT=str(self.ks), TSKS_RUN=str(self.run), TSKS_WEB=str(self.base / 'web'),
                        TSKS_PROC=str(self.base / 'proc'), MOCK_ROOT=str(self.mock), PATH=os.pathsep.join((str(self.mock), str(self.utilities))))
        (self.base / 'proc').mkdir()
        (self.base / 'proc/meminfo').write_text('MemAvailable: 131072 kB\n')
        self.checksums()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        (self.mock / name).write_text(json.dumps(value))

    def read(self, name):
        return json.loads((self.mock / name).read_text())

    def checksums(self):
        lines = []
        for path in sorted(self.pkg.rglob('*')):
            if path.is_file() and path.name != 'manifest.sha256':
                lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.relative_to(self.pkg).as_posix() + '\n')
        (self.pkg / 'manifest.sha256').write_text(''.join(lines))

    def install(self, success=True):
        # The real host scans the entire installer before executing it. Keep
        # the literal grep semantics so comments are covered as on the router.
        gate = 'a=$(grep "detect_package" "$1"); b=$(grep "ks_tar_install" "$1"); [ -z "$a" ] && [ -z "$b" ]'
        result = subprocess.run([SHELL, '-c', gate, 'host-preflight', str(self.pkg / 'install.sh')], env=self.env, text=True, capture_output=True, timeout=5)
        if result.returncode == 0:
            result = subprocess.run([SHELL, str(self.pkg / 'install.sh')], env=self.env, text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_host_preflight_stops_a_comment_match_before_plugin_execution(self):
        original = (self.pkg / 'install.sh').read_text()
        for token in ('detect_package', 'ks_tar_install'):
            (self.pkg / 'install.sh').write_text(original + '\n# Reference: ' + token + '.sh\n')
            self.checksums()
            self.install(False)
            self.assertEqual(self.calls('tsks-helper'), [])
            self.assertFalse((self.mock / 'lifecycle').exists())

    def calls(self, name):
        file = self.mock / 'calls.jsonl'
        values = [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []
        return [args for tool, args in values if tool == name]

    def legacy(self):
        (self.ks / 'bin').mkdir(exist_ok=True)
        core = binary('arm', b'legacy')
        (self.ks / 'bin/tailscale.combined').write_bytes(core)
        (self.ks / 'bin/tailscale.combined').chmod(0o755)
        for name in ('tailscale', 'tailscaled'):
            (self.ks / 'bin' / name).symlink_to('tailscale.combined')
        versions = self.read('versions.json'); versions[hashlib.sha256(core).hexdigest()] = '1.90.2'; self.write('versions.json', versions)
        (self.ks / 'configs/tailscale').mkdir(parents=True)
        (self.ks / 'configs/tailscale/tailscaled.state').write_text('preserved-identity')
        (self.ks / 'scripts').mkdir()
        (self.ks / 'scripts/tailscale_config').write_text('never execute old opaque control file')
        self.write('config.json', {'tailscale_version': '2.0.0', 'tailscale_enable': '0', 'tailscale_accept_routes': '0'})
        return core

    def current(self):
        core = self.legacy()
        target = self.ks / 'tailscale/cores/1.90.2-r1-arm'
        target.mkdir(parents=True)
        (target / 'tailscale.combined').write_bytes(core)
        (target / 'tailscale.combined').chmod(0o755)
        (target / 'descriptor.json').write_text(json.dumps(descriptor(core, 'arm', '1.90.2')))
        for name in ('tailscale', 'tailscaled'):
            (target / name).symlink_to('tailscale.combined')
        (self.ks / 'tailscale/current').symlink_to('cores/1.90.2-r1-arm')
        (self.ks / 'tailscale/previous').symlink_to('cores/1.90.2-r1-arm')
        return core

    def test_fresh_install_selects_only_local_arch_and_registers_after_start(self):
        self.install()
        self.assertEqual((self.ks / 'tailscale/current').readlink().as_posix(), 'cores/1.102.4-r1-arm')
        self.assertEqual((self.ks / 'bin/tailscale').readlink().as_posix(), '../tailscale/current/tailscale')
        self.assertTrue((self.ks / 'res/tailscale3.js').is_file())
        self.assertFalse(any('arm64' in p.name for p in self.ks.rglob('*')))
        self.assertEqual(self.read('config.json')['tailscale_version'], '3.0.0')
        self.assertEqual(self.read('config.json')['tailscale_enable'], '0')
        self.assertIn('start version=\n', (self.mock / 'lifecycle').read_text())
        self.assertFalse(list(self.ks.glob('.tailscale-install.*')))

    def test_universal_and_hnd_install_without_optional_firmware_commands(self):
        for name in ('od', 'timeout', 'command', 'sha256sum'):
            self.assertIsNone(shutil.which(name, path=self.env['PATH']))
        for package in ('universal', 'hnd'):
            with self.subTest(package=package):
                if package == 'hnd':
                    shutil.rmtree(self.ks)
                    self.ks.mkdir()
                    self.write('config.json', {})
                    (self.pkg / '.valid').write_text('hnd\n')
                    shutil.rmtree(self.pkg / 'payload/arm64')
                    self.checksums()
                result = self.install()
                self.assertNotIn('not found', result.stderr)
                self.assertIn(['elf', str(self.pkg / 'payload/arm/tailscale.combined'), 'arm'], self.calls('tsks-helper'))
                self.assertEqual(self.read('config.json')['tailscale_version'], '3.0.0')

    def test_odmpid_platform_mapping_selects_arm64_and_matching_valid(self):
        self.write('nvram.json', {'productid': 'RT-AX88U', 'odmpid': 'TX-AX6000'})
        (self.pkg / '.valid').write_text('mtk\n'); self.checksums(); self.install()
        self.assertEqual((self.ks / 'tailscale/current').readlink().as_posix(), 'cores/1.102.4-r1-arm64')
        self.assertFalse(any(p.name.endswith('-arm') for p in (self.ks / 'tailscale/cores').iterdir()))

    def test_platform_mismatch_and_old_kernel_fail_before_helper_execution(self):
        (self.pkg / '.valid').write_text('mtk\n'); self.checksums(); self.install(False)
        self.assertEqual(self.calls('tsks-helper'), [])
        (self.pkg / '.valid').write_text('hnd\n'); self.write('kernel.json', '2.6.36'); self.checksums(); self.install(False)
        self.assertEqual(self.calls('tsks-helper'), [])

    def test_bootstrap_rejects_tampered_helper_and_verifier_rejects_unlisted_files(self):
        with (self.pkg / 'payload/arm/tsks-helper').open('a') as f: f.write('\n# tampered\n')
        self.install(False); self.assertEqual(self.calls('tsks-helper'), [])
        self.checksums(); (self.pkg / 'scripts/tailscale_unlisted').write_text('bad')
        self.install(False)
        self.assertEqual(self.calls('tsks-helper'), [['check-tree', str(self.pkg)]])
        self.assertFalse((self.mock / 'lifecycle').exists())

    def test_missing_bootstrap_crypto_stops_before_helper_execution(self):
        (self.mock / 'openssl').unlink()
        result = self.install(False)
        self.assertIn('固件缺少 SHA-256 校验工具', result.stderr)
        self.assertEqual(self.calls('tsks-helper'), [])
        self.assertFalse((self.mock / 'lifecycle').exists())

    def test_native_sha256_bootstrap_works_without_openssl(self):
        (self.mock / 'openssl').unlink()
        (self.mock / 'sha256sum').symlink_to(self.mock / 'mock-command')
        self.install()
        self.assertEqual(self.calls('sha256sum'), [[str(self.pkg / 'payload/arm/tsks-helper')]])
        self.assertEqual(self.calls('openssl'), [])

    def test_traversal_manifest_and_package_symlink_are_rejected(self):
        with (self.pkg / 'manifest.sha256').open('a') as f: f.write('0' * 64 + '  ../../outside\n')
        self.install(False); self.assertEqual(self.calls('tsks-helper'), [])
        self.checksums(); (self.pkg / 'bad-link').symlink_to('/etc/passwd')
        self.install(False); self.assertEqual(self.calls('tsks-helper'), [])

    def test_signed_core_hash_and_arch_are_validated_before_stop(self):
        core = self.pkg / 'payload/arm/tailscale.combined'
        core.write_bytes(binary('arm', b'tampered')); self.checksums()
        result = self.install(False)
        self.assertIn('核心 SHA-256 校验失败', result.stderr)
        self.assertNotIn('签名', result.stderr)
        self.assertFalse((self.mock / 'lifecycle').exists())
        data = json.loads((self.pkg / 'release.json').read_text())
        data['arm'] = descriptor(binary('arm64'), 'arm')
        core.write_bytes(binary('arm64')); (self.pkg / 'release.json').write_text(json.dumps(data)); self.checksums()
        result = self.install(False)
        self.assertIn('核心 ELF 格式或架构不匹配', result.stderr)
        self.assertNotIn('签名', result.stderr)
        self.assertFalse((self.mock / 'lifecycle').exists())

    def test_unexecutable_and_unexpected_core_versions_have_distinct_errors(self):
        self.write('versions.json', {})
        result = self.install(False)
        self.assertIn('核心无法执行或读取版本超时', result.stderr)
        self.assertNotIn('签名', result.stderr)
        self.assertFalse((self.mock / 'lifecycle').exists())
        digest = hashlib.sha256(binary('arm')).hexdigest()
        self.write('versions.json', {digest: '1.100.0'})
        result = self.install(False)
        self.assertIn('核心实际版本与描述不一致', result.stderr)
        self.assertNotIn('签名', result.stderr)
        self.assertFalse((self.mock / 'lifecycle').exists())

    def test_bad_signature_fails_without_mutating_existing_install(self):
        old = self.legacy()
        signed = json.loads((self.pkg / 'release.json').read_text()); signed['valid'] = False
        (self.pkg / 'release.json').write_text(json.dumps(signed)); self.checksums()
        result = self.install(False)
        self.assertIn('核心签名校验失败', result.stderr)
        self.assertEqual((self.ks / 'bin/tailscale.combined').read_bytes(), old)
        self.assertEqual(self.read('config.json')['tailscale_version'], '2.0.0')
        self.assertFalse((self.mock / 'lifecycle').exists())

    def test_legacy_upgrade_preserves_actual_core_preferences_and_identity(self):
        old = self.legacy(); self.install()
        target = self.ks / 'tailscale/current'
        self.assertEqual(target.readlink().as_posix(), 'cores/1.90.2-legacy-arm')
        self.assertEqual((target / 'tailscale.combined').read_bytes(), old)
        self.assertEqual(json.loads((target / 'descriptor.json').read_text())['origin'], 'legacy')
        self.assertEqual((self.ks / 'configs/tailscale/tailscaled.state').read_text(), 'preserved-identity')
        self.assertEqual(self.read('config.json')['tailscale_accept_routes'], '0')
        self.assertFalse((self.ks / 'bin/tailscale.combined').exists())
        self.assertIn('start version=2.0.0', (self.mock / 'lifecycle').read_text())

    def test_current_core_and_previous_link_are_retained_on_plugin_upgrade(self):
        old = self.current(); self.install()
        self.assertEqual((self.ks / 'tailscale/current').readlink().as_posix(), 'cores/1.90.2-r1-arm')
        self.assertEqual((self.ks / 'tailscale/previous').readlink().as_posix(), 'cores/1.90.2-r1-arm')
        self.assertEqual((self.ks / 'tailscale/current/tailscale.combined').read_bytes(), old)
        self.assertFalse((self.ks / 'tailscale/cores/1.102.4-r1-arm').exists())

    def test_start_failure_rolls_back_files_dbus_state_and_core_links(self):
        old = self.legacy(); before = self.read('config.json')
        (self.mock / 'fail-start').touch(); self.install(False)
        self.assertEqual((self.ks / 'bin/tailscale.combined').read_bytes(), old)
        self.assertEqual((self.ks / 'bin/tailscaled').readlink().as_posix(), 'tailscale.combined')
        self.assertEqual((self.ks / 'scripts/tailscale_config').read_text(), 'never execute old opaque control file')
        self.assertEqual((self.ks / 'configs/tailscale/tailscaled.state').read_text(), 'preserved-identity')
        self.assertEqual(self.read('config.json'), before)
        self.assertFalse((self.ks / 'tailscale/current').exists())
        self.assertFalse((self.ks / 'res/tailscale3.js').exists())
        self.assertFalse(list(self.ks.glob('.tailscale-install.*')))

    def test_shared_lifecycle_lock_blocks_install_without_stopping_service(self):
        with (self.run / 'operation.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.install(False)
        self.assertFalse((self.mock / 'lifecycle').exists())
        self.assertEqual(self.calls('tsks-helper'), [['check-tree', str(self.pkg)]])

    def test_pending_core_transaction_blocks_install_before_copies(self):
        old = self.legacy(); before = self.read('config.json')
        journal = self.ks / 'tailscale/update.txn'; journal.parent.mkdir()
        journal.write_text('{"phase":"switching","old":"cores/old"}')
        result = self.install(False)
        self.assertIn('recovery_required', result.stderr)
        self.assertEqual(journal.read_text(), '{"phase":"switching","old":"cores/old"}')
        self.assertEqual((self.ks / 'bin/tailscale.combined').read_bytes(), old)
        self.assertEqual(self.read('config.json'), before)
        self.assertEqual(self.calls('tsks-helper'), [['check-tree', str(self.pkg)]])
        self.assertEqual(self.calls('mv'), [])
        self.assertFalse((self.mock / 'lifecycle').exists())
        self.assertFalse(list(self.ks.glob('.tailscale-install.*')))

    def test_flash_and_ram_preflight_fail_before_copy_or_stop(self):
        self.write('space.json', 4096); self.install(False)
        self.assertEqual(self.calls('tsks-helper'), [['check-tree', str(self.pkg)]])
        self.assertFalse((self.mock / 'lifecycle').exists())
        self.write('space.json', 1048576)
        (self.base / 'proc/meminfo').write_text('MemAvailable: 8192 kB\n')
        self.install(False)
        self.assertEqual(self.calls('tsks-helper'), [['check-tree', str(self.pkg)]] * 2)
        self.assertFalse((self.mock / 'lifecycle').exists())

    def test_partial_file_replacement_failure_restores_original_install(self):
        old = self.legacy(); before = self.read('config.json')
        (self.mock / 'fail-move').touch(); self.install(False)
        self.assertEqual((self.ks / 'bin/tailscale.combined').read_bytes(), old)
        self.assertEqual((self.ks / 'scripts/tailscale_config').read_text(), 'never execute old opaque control file')
        self.assertEqual(self.read('config.json'), before)
        self.assertFalse((self.ks / 'res/tailscale3.js').exists())
        self.assertFalse(list(self.ks.glob('.tailscale-install.*')))

    def test_single_dash_legacy_argv_stops_only_the_matching_state_process(self):
        self.legacy()
        wanted = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        waiter = threading.Thread(target=wanted.wait, daemon=True); waiter.start()
        try:
            for proc, state in ((wanted, self.ks / 'configs/tailscale/tailscaled.state'), (unrelated, self.base / 'other.state')):
                folder = self.base / 'proc' / str(proc.pid); folder.mkdir(parents=True)
                argv = [str(self.ks / 'bin/tailscaled'), '-state', str(state), '-socket', str(self.base / 'old.sock')]
                (folder / 'cmdline').write_bytes(b'\0'.join(part.encode() for part in argv) + b'\0')
            self.install()
            waiter.join(2)
            self.assertIsNotNone(wanted.poll())
            self.assertIsNone(unrelated.poll())
        finally:
            for proc in (wanted, unrelated):
                if proc.poll() is None: proc.terminate()
            unrelated.wait(timeout=5); waiter.join(5)

    def test_failed_legacy_upgrade_restarts_original_socket_and_default_port(self):
        self.legacy()
        daemon = self.ks / 'bin/tailscaled'; daemon.unlink()
        daemon.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" >"$MOCK_ROOT/legacy-argv"\n')
        daemon.chmod(0o755)
        config = self.read('config.json'); config['tailscale_enable'] = '1'; self.write('config.json', config)
        (self.mock / 'fail-start').touch(); self.install(False)
        # Fixture readiness is mocked, so wait for the detached argv recorder.
        for unused in range(100):
            if (self.mock / 'legacy-argv').exists(): break
            time.sleep(0.01)
        # The old daemon had no explicit port: retain its own default.
        argv = (self.mock / 'legacy-argv').read_text().splitlines()
        self.assertEqual(argv, ['--state=' + str(self.ks / 'configs/tailscale/tailscaled.state'), '--socket=/var/run/tailscale/tailscaled.sock'])
        self.assertIn(['status', '/var/run/tailscale/tailscaled.sock'], self.calls('tsks-helper'))
        self.assertIn('firewall', (self.mock / 'lifecycle').read_text())
        self.assertEqual(self.read('config.json'), config)

    def test_failed_fresh_start_preserves_newly_generated_identity(self):
        (self.mock / 'fail-start').touch(); self.install(False)
        self.assertEqual((self.ks / 'configs/tailscale/tailscaled.state').read_text(), 'damaged')
        self.assertFalse((self.ks / 'tailscale/current').exists())
        self.assertNotIn('tailscale_version', self.read('config.json'))

    def test_license_is_installed_and_scripts_need_no_command_builtin(self):
        (self.pkg / 'res/LICENSE-tailscale.txt').write_text('license notice')
        self.checksums(); self.install()
        self.assertEqual((self.ks / 'res/LICENSE-tailscale.txt').read_text(), 'license notice')
        self.assertNotIn('command -v', (self.pkg / 'install.sh').read_text())
        self.assertNotIn('command -v', (self.pkg / 'uninstall.sh').read_text())

    def test_uninstall_preserves_identity_preferences_and_unrelated_files(self):
        self.legacy(); self.install()
        unrelated = self.ks / 'scripts/tailscale_custom_user_note'; unrelated.write_text('keep')
        result = subprocess.run([SHELL, str(self.ks / 'scripts/uninstall_tailscale.sh')], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.ks / 'configs/tailscale/tailscaled.state').read_text(), 'preserved-identity')
        self.assertEqual(self.read('config.json')['tailscale_accept_routes'], '0')
        self.assertNotIn('tailscale_version', self.read('config.json'))
        self.assertTrue(unrelated.exists())
        self.assertFalse((self.ks / 'bin/tsks-helper').exists())
        self.assertFalse((self.ks / 'tailscale/cores').exists())

    def test_uninstall_preserves_pending_recovery_and_installed_files(self):
        self.legacy(); self.install()
        journal = self.ks / 'tailscale/update.txn'
        journal.write_text('{"phase":"switched"}')
        result = subprocess.run([SHELL, str(self.ks / 'scripts/uninstall_tailscale.sh')], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(journal.exists())
        self.assertTrue((self.ks / 'bin/tsks-helper').exists())
        self.assertEqual((self.ks / 'configs/tailscale/tailscaled.state').read_text(), 'preserved-identity')


if __name__ == '__main__':
    unittest.main()
