#!/usr/bin/env python3
"""Offline tests for tools/package_release.sh's build and smoke isolation.

The script is run inside a throwaway copy of the repo layout it reads, with
fake `cargo` / `go` shims, so no Rust or Go toolchain, network, or real
release build is involved. The fake cargo honours CARGO_TARGET_DIR exactly
like real cargo does, and the fake hyb it "builds" records the environment
the smoke test hands it.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "package_release.sh"

# The hyb the fake cargo emits: `build <src> -o <out>` writes a hello program
# and logs the env vars hyb itself consults (main.rs: HYB_RUNTIME_DIR,
# HYB_CARGO_BIN, CARGO_TARGET_DIR) so the test can see what the smoke leaked.
FAKE_HYB = r"""#!/bin/sh
# BUILD_MARK
log="${HYB_TEST_LOG:?}"
printf '{"runtime": "%s", "cargo_bin": "%s", "target_dir": "%s", "exe": "%s"}\n' \
    "${HYB_RUNTIME_DIR-<unset>}" "${HYB_CARGO_BIN-<unset>}" \
    "${CARGO_TARGET_DIR-<unset>}" "$0" >> "$log"
out=""
while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then out="$2"; shift; fi
    shift
done
printf '#!/bin/sh\necho "hello, Hybrid!"\n' > "$out"
chmod +x "$out"
"""

# Fake cargo: writes the artifact under ${CARGO_TARGET_DIR:-./target}, the
# way real cargo resolves its output directory.
FAKE_CARGO = r"""#!/bin/sh
target="${CARGO_TARGET_DIR:-$PWD/target}"
triple=""
pkg=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) triple="$2"; shift ;;
        -p) pkg="$2"; shift ;;
    esac
    shift
done
dir="$target/${triple:+$triple/}release"
mkdir -p "$dir"
if [ "$pkg" = "hyb" ]; then
    sed 's/BUILD_MARK/FRESH-HYB/' "$HYB_TEST_FAKE_HYB" > "$dir/hyb"
    chmod +x "$dir/hyb"
else
    echo FRESH-LIB > "$dir/libhyrt.a"
fi
"""


class PackageReleaseIsolationTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("sh") or not shutil.which("tar"):
            self.skipTest("needs sh and tar")
        tmp = tempfile.TemporaryDirectory(prefix="hybrid-package-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "tools").mkdir(parents=True)
        shutil.copy(SCRIPT, self.repo / "tools" / "package_release.sh")
        (self.repo / "tools" / "RELEASE-INSTALL.md").write_text("install notes\n")
        (self.repo / "runtime" / "go" / "hybridge").mkdir(parents=True)
        (self.repo / "runtime" / "go" / "hybridge" / "doc.go").write_text("package hybridge\n")
        hyrt = self.repo / "runtime" / "rust" / "hyrt"
        (hyrt / "src").mkdir(parents=True)
        (hyrt / "Cargo.toml").write_text("[package]\nname = \"hyrt\"\n")
        (hyrt / "src" / "lib.rs").write_text("// hyrt\n")
        (self.repo / "compiler").mkdir()
        (self.repo / "examples" / "01-hello").mkdir(parents=True)
        (self.repo / "examples" / "01-hello" / "program.hy").write_text("fn main() {}\n")

        # The stale-archive trap (worklog session 8): old artifacts sit at the
        # crate-local paths the script copies from.
        (hyrt / "target" / "release").mkdir(parents=True)
        (hyrt / "target" / "release" / "libhyrt.a").write_text("STALE-LIB\n")
        stale_hyb = self.repo / "compiler" / "target" / "release" / "hyb"
        stale_hyb.parent.mkdir(parents=True)
        stale_hyb.write_text(FAKE_HYB.replace("BUILD_MARK", "STALE-HYB"))
        stale_hyb.chmod(0o755)

        fake_hyb = self.root / "fake_hyb.sh"
        fake_hyb.write_text(FAKE_HYB)
        # cargo lives under a .cargo/bin dir so the smoke's PATH filter drops it
        tools = self.root / "home" / ".cargo" / "bin"
        tools.mkdir(parents=True)
        (tools / "cargo").write_text(FAKE_CARGO)
        (tools / "cargo").chmod(0o755)
        gobin = self.root / "gobin"
        gobin.mkdir()
        (gobin / "go").write_text("#!/bin/sh\nexit 0\n")
        (gobin / "go").chmod(0o755)

        self.log = self.root / "hyb-env.log"
        self.env = {
            "PATH": os.pathsep.join([str(tools), str(gobin), os.defpath]),
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root),
            "HYB_TEST_LOG": str(self.log),
            "HYB_TEST_FAKE_HYB": str(fake_hyb),
            "HYB_PKG_OS": "linux",
            "HYB_PKG_ARCH": "amd64",
        }

    def package(self, **extra):
        result = subprocess.run(
            ["sh", str(self.repo / "tools" / "package_release.sh"), "v9.9.9-test"],
            env={**self.env, **extra}, cwd=self.root,
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def shipped(self, member):
        tarball = self.repo / "dist" / "hybrid-v9.9.9-test-linux-amd64.tar.gz"
        with tarfile.open(tarball) as tar:
            return tar.extractfile(f"hybrid-v9.9.9-test/{member}").read().decode()

    def smoke_env(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_clean_env_ships_fresh_artifacts(self):
        self.package()
        self.assertEqual(self.shipped("runtime/rust/hyrt/target/release/libhyrt.a"), "FRESH-LIB\n")
        self.assertIn("FRESH-HYB", self.shipped("hyb"))

    def test_shared_cargo_target_dir_does_not_ship_stale_artifacts(self):
        # env.sh exports CARGO_TARGET_DIR for every session; cargo then writes
        # there, while the script copies the crate-local (stale) paths.
        shared = self.root / "shared-target"
        self.package(CARGO_TARGET_DIR=str(shared))
        self.assertEqual(self.shipped("runtime/rust/hyrt/target/release/libhyrt.a"), "FRESH-LIB\n")
        self.assertIn("FRESH-HYB", self.shipped("hyb"))

    def test_smoke_runs_only_the_tarball_runtime(self):
        # A caller's runtime override, cargo override, or shared target dir
        # would let the smoke pass on something that is not in the tarball.
        elsewhere = self.root / "dev-runtime"
        self.package(
            HYB_RUNTIME_DIR=str(elsewhere),
            HYB_CARGO_BIN=str(self.root / "home" / ".cargo" / "bin" / "cargo"),
            CARGO_TARGET_DIR=str(self.root / "shared-target"),
        )
        runs = self.smoke_env()
        self.assertTrue(runs, "the smoke never ran hyb")
        for run in runs:
            self.assertEqual(run["runtime"], "<unset>", run)
            self.assertEqual(run["cargo_bin"], "<unset>", run)
            self.assertEqual(run["target_dir"], "<unset>", run)
            self.assertIn("/hybrid-v9.9.9-test/hyb", run["exe"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
