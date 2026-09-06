#!/usr/bin/env python3

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class RunBazelCiTest(unittest.TestCase):
    def invoke(self, *, runner="Windows", options=(), args=(), key="", status=0):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "run-bazel-ci.sh"
            shutil.copyfile(Path(__file__).with_name(script.name), script)
            runner_script = root / "run_bazel_with_buildbuddy.py"
            runner_script.write_text(
                '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n'
                'exit "$BAZEL_STATUS"\n',
                encoding="utf-8",
            )
            runner_script.chmod(0o755)
            capture = root / "args.txt"
            env = {
                "PATH": os.environ["PATH"],
                "RUNNER_OS": runner,
                "CODEX_BAZEL_WINDOWS_PATH": "C:/Windows;C:/tools",
                "BUILDBUDDY_API_KEY": key,
                "CAPTURE_ARGS": str(capture),
                "BAZEL_STATUS": str(status),
            }
            result = subprocess.run(
                ["bash", str(script), *options, "--", "test", *args, "--", "//:test"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, status, result.stderr)
            return capture.read_text(encoding="utf-8").splitlines()

    def test_keyless_cross_build_keeps_gnu_target_and_native_host(self):
        self.assertEqual(
            self.invoke(options=("--windows-cross-compile",)),
            [
                "--noexperimental_remote_repo_contents_cache",
                "test",
                "--host_platform=//:local_windows_msvc",
                "--platforms=//:windows_x86_64_gnullvm",
                "--config=windows-gnullvm-tests",
                "--extra_toolchains=//:windows_gnullvm_tests_on_msvc_host_toolchain",
                "--jobs=8",
                "--action_env=PATH=C:/Windows;C:/tools",
                "--host_action_env=PATH=C:/Windows;C:/tools",
                "--test_env=PATH=C:/Windows;C:/tools",
                "--",
                "//:test",
            ],
        )

    def test_explicit_target_platform_is_preserved(self):
        for args in (
            ("--platforms=//:windows_x86_64_msvc",),
            ("--platforms", "//:windows_x86_64_msvc"),
        ):
            with self.subTest(args=args):
                actual = self.invoke(options=("--windows-cross-compile",), args=args)
                self.assertEqual(actual[2 : 2 + len(args)], list(args))
                self.assertNotIn("--platforms=//:windows_x86_64_gnullvm", actual)
                self.assertNotIn("--config=windows-gnullvm-tests", actual)

    def test_explicit_gnu_target_keeps_gnu_runtime_settings(self):
        actual = self.invoke(
            options=("--windows-cross-compile",),
            args=("--platforms=//:local_windows",),
        )
        self.assertIn("--config=windows-gnullvm-tests", actual)

    def test_authenticated_cross_build_still_uses_remote_configuration(self):
        self.assertEqual(
            self.invoke(options=("--windows-cross-compile",), key="test-key"),
            [
                "--noexperimental_remote_repo_contents_cache",
                "test",
                "--config=ci-windows-cross",
                "--host_platform=//:rbe",
                "--shell_executable=/bin/bash",
                "--action_env=PATH=/usr/bin:/bin",
                "--host_action_env=PATH=/usr/bin:/bin",
                "--test_env=PATH=C:/Windows;C:/tools",
                "--",
                "//:test",
            ],
        )

    def test_other_builds_do_not_force_a_target_abi(self):
        for runner in ("Windows", "Linux", "macOS"):
            with self.subTest(runner=runner):
                actual = self.invoke(runner=runner)
                self.assertFalse(any(arg.startswith("--platforms=") for arg in actual))
                self.assertFalse(
                    any(arg.startswith("--extra_toolchains=") for arg in actual)
                )

    def test_bazel_failure_status_is_preserved(self):
        self.invoke(options=("--windows-cross-compile",), status=42)


if __name__ == "__main__":
    unittest.main()
