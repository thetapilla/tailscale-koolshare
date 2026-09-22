#!/usr/bin/env python3
"""Build original BusyBox 1.25.1 ash and execute isolated backend fixtures."""
import argparse
from pathlib import Path
import subprocess

from build_core import RECIPE, download

ROOT = Path(__file__).resolve().parents[1]
BUSYBOX_SHA256 = "27667e0f2328fdbd79cfd622e4453e5c57e58f781c5da97c9be337d93aa2a02e"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "backend", "core", "install"), default="all")
    args = parser.parse_args()
    archive = ROOT / ".cache/downloads/busybox-1.25.1.tar.bz2"
    download("https://busybox.net/downloads/busybox-1.25.1.tar.bz2", archive, BUSYBOX_SHA256)
    build_script = r'''
set -eu
if [ ! -f /out/busybox-1.25.1/fixture-v2.ready ]; then
    apt-get update -qq
    apt-get install -y --no-install-recommends gcc make libc6-dev bzip2
    mkdir -p /out/busybox-1.25.1
    tar -xjf /cache/downloads/busybox-1.25.1.tar.bz2 -C /out/busybox-1.25.1 --strip-components=1
    cd /out/busybox-1.25.1
    make allnoconfig
    for option in ASH ASH_JOB_CONTROL ASH_ALIAS ASH_GETOPTS ASH_BUILTIN_ECHO ASH_BUILTIN_PRINTF ASH_BUILTIN_TEST ASH_CMDCMD SH_MATH_SUPPORT SH_MATH_SUPPORT_64 FEATURE_SH_IS_ASH; do
        sed -i "s/# CONFIG_${option} is not set/CONFIG_${option}=y/" .config
    done
    sed -i 's/CONFIG_FEATURE_SH_IS_NONE=y/# CONFIG_FEATURE_SH_IS_NONE is not set/' .config
    yes '' | make oldconfig
    make -j4 busybox
    ./busybox ash -c 'test "$((2 + 3))" = 5 && command -v printf >/dev/null'
    touch fixture-v2.ready
fi
'''
    test_script = r'''
set -eu
/out/busybox-1.25.1/busybox ash -c 'test "$((2 + 3))" = 5 && command -v printf >/dev/null'
printf 'Testing original BusyBox 1.25.1 ash from the SHA-256-pinned source archive\n'
ln -sf busybox /out/busybox-1.25.1/sh
find /repo/plugin -type f \( -path '*/scripts/*' -o -name '*.sh' \) -exec sh -c 'for file do /out/busybox-1.25.1/sh -n "$file" || exit 1; done' sh {} +
cd /repo
for pattern in $TSKS_TEST_PATTERNS; do
    TSKS_TEST_SHELL=/out/busybox-1.25.1/sh python3 -m unittest discover -s tests -p "$pattern" -v
done
'''
    mounts = [
                    "--mount", f"type=bind,src={ROOT / 'plugin'},dst=/repo/plugin,readonly",
                    "--mount", f"type=bind,src={ROOT / 'tests'},dst=/repo/tests,readonly",
                    "--mount", f"type=bind,src={ROOT / '.cache'},dst=/cache,readonly",
                    "--mount", f"type=bind,src={ROOT / 'build'},dst=/out"]
    if not (ROOT / "build/busybox-1.25.1/fixture-v2.ready").exists():
        subprocess.run(["docker", "run", "--rm", "--security-opt=no-new-privileges", *mounts,
                        RECIPE["container"], "sh", "-c", build_script], check=True)
    suites = ("backend", "core", "install") if args.suite == "all" else (args.suite,)
    patterns = " ".join("test_" + suite + ".py" for suite in suites)
    subprocess.run(["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges", *mounts,
                    "-e", "TSKS_TEST_PATTERNS=" + patterns,
                    RECIPE["container"], "sh", "-c", test_script], check=True)


if __name__ == "__main__":
    main()
