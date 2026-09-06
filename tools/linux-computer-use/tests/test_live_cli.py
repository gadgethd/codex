import asyncio
import signal
import sys
import unittest

from live.cli_scenario import wait_for_cli


class CliCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_and_cancellation_reap_a_client_ignoring_termination(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-u",
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    "print('ready'); time.sleep(60)",
                    stdout=asyncio.subprocess.PIPE,
                )
                try:
                    self.assertEqual(
                        await asyncio.wait_for(proc.stdout.readline(), 5), b"ready\n"
                    )
                    waiter = asyncio.create_task(
                        wait_for_cli(
                            proc,
                            timeout=60 if cancel else 0.05,
                            termination_timeout=0.05,
                        )
                    )
                    if cancel:
                        await asyncio.sleep(0)
                        waiter.cancel()
                    error = asyncio.CancelledError if cancel else asyncio.TimeoutError
                    with self.assertRaises(error):
                        await waiter
                    self.assertEqual(proc.returncode, -signal.SIGKILL)
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
