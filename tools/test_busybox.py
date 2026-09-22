#!/usr/bin/env python3
"""Build BusyBox 1.25.1 ash/applets and execute isolated backend fixtures."""
import argparse
from pathlib import Path
import subprocess

from build_core import RECIPE, download

ROOT = Path(__file__).resolve().parents[1]
BUSYBOX_SHA256 = "27667e0f2328fdbd79cfd622e4453e5c57e58f781c5da97c9be337d93aa2a02e"
PROFILE_CHECK = r'''
assert_fixture_profile() {
    fixture_root=$1
    for option in ASH_CMDCMD FEATURE_SH_STANDALONE OD TIMEOUT MKFIFO MKTEMP; do
        grep -qx "# CONFIG_${option} is not set" "$fixture_root/.config" || {
            printf 'Unsupported fixture capability is enabled: %s\n' "$option" >&2
            exit 1
        }
    done
    PATH=/nonexistent "$fixture_root/busybox" ash -c '
        test "$((2 + 3))" = 5 || exit 1
        for name in command od timeout mkfifo mktemp; do
            "$name" --help >/dev/null 2>&1
            [ "$?" = 127 ] || exit 1
        done
    '
    for name in od timeout mkfifo mktemp; do
        applet_status=0
        "$fixture_root/busybox" "$name" --help >/dev/null 2>&1 || applet_status=$?
        [ "$applet_status" = 127 ] || {
            printf 'Unsupported fixture applet is available: %s\n' "$name" >&2
            exit 1
        }
    done
    printf 'match\n' | "$fixture_root/busybox" grep -Eq '^(match|other)$'
    [ "$("$fixture_root/busybox" awk 'BEGIN {print int(3.5), sqrt(4)}')" = "3 2" ]
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "backend", "core", "install"), default="all")
    args = parser.parse_args()
    (ROOT / "build").mkdir(parents=True, exist_ok=True)
    archive = ROOT / ".cache/downloads/busybox-1.25.1.tar.bz2"
    download("https://busybox.net/downloads/busybox-1.25.1.tar.bz2", archive, BUSYBOX_SHA256)
    build_script = PROFILE_CHECK + r'''
set -eu
if [ ! -f /out/busybox-1.25.1/fixture-v5.ready ]; then
    apt-get update -qq
    apt-get install -y --no-install-recommends gcc make libc6-dev bzip2
    mkdir -p /out/busybox-1.25.1
    tar -xjf /cache/downloads/busybox-1.25.1.tar.bz2 -C /out/busybox-1.25.1 --strip-components=1
    cd /out/busybox-1.25.1
    # glibc 2.31 removed stime. Adapt only the fixture's date-setting syscall;
    # its parser, formatting and all plugin-facing commands remain unchanged.
    python3 - <<'PY'
from pathlib import Path
date_source = Path("coreutils/date.c")
source = date_source.read_text()
old = "stime(&ts.tv_sec)"
new = "clock_settime(CLOCK_REALTIME, &(struct timespec){ .tv_sec = ts.tv_sec, .tv_nsec = 0 })"
if source.count(old) != 1:
    raise SystemExit("BusyBox date compatibility patch no longer matches the pinned source")
date_source.write_text(source.replace(old, new))
PY
    make allnoconfig
    for option in ASH ASH_JOB_CONTROL ASH_ALIAS ASH_GETOPTS ASH_BUILTIN_ECHO ASH_BUILTIN_PRINTF ASH_BUILTIN_TEST SH_MATH_SUPPORT SH_MATH_SUPPORT_64 FEATURE_SH_IS_ASH AWK FEATURE_AWK_LIBM CAT CHMOD CP DATE DF DIRNAME FIND FEATURE_FIND_TYPE GREP FEATURE_GREP_EGREP_ALIAS FEATURE_GREP_FGREP_ALIAS LN LS FEATURE_LS_TIMESTAMPS FEATURE_LS_SORTFILES MKDIR MV READLINK RM RMDIR SED SHA256SUM FEATURE_MD5_SHA1_SUM_CHECK SLEEP SYNC TAIL FEATURE_FANCY_TAIL TR UNAME WC WHICH; do
        sed -i "s/# CONFIG_${option} is not set/CONFIG_${option}=y/" .config
    done
    sed -i 's/CONFIG_FEATURE_SH_IS_NONE=y/# CONFIG_FEATURE_SH_IS_NONE is not set/' .config
    yes '' | make oldconfig
    make -j4 busybox
    assert_fixture_profile /out/busybox-1.25.1
    touch fixture-v5.ready
fi
'''
    test_script = PROFILE_CHECK + r'''
set -eu
assert_fixture_profile /out/busybox-1.25.1
printf 'Testing BusyBox 1.25.1 ash and utilities with isolated fixture PATHs\n'
ln -sf busybox /out/busybox-1.25.1/sh
find /repo/plugin -type f \( -path '*/scripts/*' -o -name '*.sh' \) -exec sh -c 'for file do /out/busybox-1.25.1/sh -n "$file" || exit 1; done' sh {} +
cd /repo
for pattern in $TSKS_TEST_PATTERNS; do
    TSKS_TEST_SHELL=/out/busybox-1.25.1/sh TSKS_TEST_BUSYBOX=/out/busybox-1.25.1/busybox python3 -m unittest discover -s tests -p "$pattern" -v
done
'''
    mounts = [
                    "--mount", f"type=bind,src={ROOT / 'plugin'},dst=/repo/plugin,readonly",
                    "--mount", f"type=bind,src={ROOT / 'tests'},dst=/repo/tests,readonly",
                    "--mount", f"type=bind,src={ROOT / '.cache'},dst=/cache,readonly",
                    "--mount", f"type=bind,src={ROOT / 'build'},dst=/out"]
    if not (ROOT / "build/busybox-1.25.1/fixture-v5.ready").exists():
        subprocess.run(["docker", "run", "--rm", "--security-opt=no-new-privileges", *mounts,
                        RECIPE["container"], "sh", "-c", build_script], check=True)
    suites = ("backend", "core", "install") if args.suite == "all" else (args.suite,)
    patterns = " ".join("test_" + suite + ".py" for suite in suites)
    subprocess.run(["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges", *mounts,
                    "-e", "TSKS_TEST_PATTERNS=" + patterns,
                    RECIPE["container"], "sh", "-c", test_script], check=True)


if __name__ == "__main__":
    main()
