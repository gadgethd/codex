import os
import subprocess
import sys
import unittest

from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.policy import LinuxPolicy
from codex_linux_computer_use.worker_policy import policy_file, read_policy


class WorkerPolicyTests(unittest.TestCase):
    def test_large_effective_policy_reaches_child_without_command_line_payload(self):
        policy = LinuxPolicy(
            True, "deny", {"\x01" * 505 + f"{i:07}": "allow" for i in range(256)}, False
        )
        with policy_file(policy) as descriptor:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from codex_linux_computer_use.worker_policy import read_policy; p=read_policy(int(sys.argv[1])); print(len(p.desktop_ids), p.default_app_access)",
                    str(descriptor),
                ],
                pass_fds=(descriptor,),
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            self.assertEqual(result.stdout, "256 deny\n")
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        with policy_file(None) as descriptor, self.assertRaises(PortalError):
            read_policy(descriptor)
