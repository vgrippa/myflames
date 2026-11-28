"""Output-aware client limits, exercised with real local pipes and children.

The fake Shell never connects to a database. Only connector's monotonic
clock is scaled; subprocess, threading and child delays keep their real
clocks so cleanup and the test harness remain independently bounded.
"""
from contextlib import contextmanager
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

from myflames import connector


class TestOutputIdleLimit(unittest.TestCase):
    SCALE = 1000.0

    @contextmanager
    def child(self, script, scaled=True):
        """Record the actual client and clean it up even if an assertion fails."""
        children = []
        real_popen = subprocess.Popen
        origin = time.monotonic()
        clock = types.SimpleNamespace(monotonic=lambda: (time.monotonic() - origin) * self.SCALE)

        def launch(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc

        try:
            with mock.patch.object(subprocess, 'Popen', side_effect=launch):
                if scaled:
                    with mock.patch.object(connector, 'time', clock), \
                            mock.patch.object(connector, 'PROCESS_CLEANUP_TIMEOUT', self.SCALE):
                        yield [sys.executable, '-c', script], children, clock
                else:
                    yield [sys.executable, '-c', script], children, time
        finally:
            for proc in children:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=3)
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()

    def assert_reaped(self, children):
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode, 'transport must reap the client before returning')
        self.assertTrue(all(pipe is None or pipe.closed for pipe in
                            (children[0].stdin, children[0].stdout, children[0].stderr)))

    def test_fake_shell_progress_runs_longer_than_old_600_second_limit(self):
        # One real millisecond represents one simulated second. The fake
        # Shell reports every five simulated seconds for at least 900 s.
        script = ('import sys, time\n'
                  'assert sys.stdin.buffer.read() == b"fake clone"\n'
                  'for step in range(180):\n'
                  ' print("Cloning: %d" % step, flush=True)\n'
                  ' time.sleep(0.005)\n'
                  'print("Clone finished", flush=True)\n')
        with self.child(script) as (argv, children, clock):
            started = clock.monotonic()
            result = connector._run_bounded(argv, timeout=5000, idle_timeout=250, input=b'fake clone')
            self.assertGreater(clock.monotonic() - started, 600)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.count(b'Cloning:'), 180)
            self.assertTrue(result.stdout.endswith(b'Clone finished\n'))
            self.assertEqual(result.stderr, b'')
            self.assert_reaped(children)

    def test_either_pipe_resets_idle_without_complete_lines_or_display_callback(self):
        for descriptor in (1, 2):
            # These raw CR updates have no newline and no on_output hook.
            # A stdout-only or line-based activity detector would expire.
            script = ('import os, time\n'
                      'for step in range(100):\n'
                      ' os.write(%d, b".\\r")\n'
                      ' time.sleep(0.005)\n' % descriptor)
            with self.subTest(descriptor=descriptor), self.child(script) as (argv, children, clock):
                result = connector._run_bounded(argv, timeout=5000, idle_timeout=250)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout if descriptor == 1 else result.stderr, b'.\r' * 100)
                self.assertEqual(result.stderr if descriptor == 1 else result.stdout, b'')
                self.assert_reaped(children)

    def test_silence_expires_and_retains_both_pipes_after_reaping(self):
        script = ('import os\n'
                  'os.write(1, b"Clone started\\n")\n'
                  'os.write(2, b"Restart pending\\r")\n'
                  'reader, writer = os.pipe()\n'
                  'os.read(reader, 1)\n')
        with self.child(script) as (argv, children, clock):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                connector._run_bounded(argv, timeout=5000, idle_timeout=250)
            self.assertEqual(caught.exception.idle_timeout, 250)
            self.assertEqual(caught.exception.timeout, 250)
            self.assertEqual(caught.exception.output, b'Clone started\n')
            self.assertEqual(caught.exception.stderr, b'Restart pending\r')
            self.assert_reaped(children)

    def test_overall_ceiling_expires_despite_continuing_output(self):
        script = ('import os, time\n'
                  'while True:\n'
                  ' os.write(1, b"progress\\r")\n'
                  ' time.sleep(0.005)\n')
        with self.child(script) as (argv, children, clock):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                connector._run_bounded(argv, timeout=650, idle_timeout=250)
            self.assertIsNone(getattr(caught.exception, 'idle_timeout', None))
            self.assertEqual(caught.exception.timeout, 650)
            self.assertIn(b'progress\r', caught.exception.output)
            self.assertEqual(caught.exception.stderr, b'')
            self.assert_reaped(children)

    def test_idle_still_expires_when_both_output_pipes_close_before_process_exit(self):
        script = ('import os\n'
                  'os.write(1, b"Closing output\\n")\n'
                  'os.close(1)\n'
                  'os.close(2)\n'
                  'reader, writer = os.pipe()\n'
                  'os.read(reader, 1)\n')
        with self.child(script) as (argv, children, clock):
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                connector._run_bounded(argv, timeout=5000, idle_timeout=250)
            self.assertEqual(caught.exception.idle_timeout, 250)
            self.assertEqual(caught.exception.output, b'Closing output\n')
            self.assertLess(time.monotonic() - started, 3, 'closed pipes must not bypass the idle deadline')
            self.assert_reaped(children)

    def test_spawn_callback_runs_once_before_any_pipe_worker_starts(self):
        script = 'import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n'
        trace = []
        real_thread = threading.Thread

        def pipe_worker(*args, **kwargs):
            self.assertEqual(trace, ['spawned'])
            return real_thread(*args, **kwargs)

        with self.child(script) as (argv, children, clock), \
                mock.patch.object(threading, 'Thread', side_effect=pipe_worker):
            callback = mock.Mock(side_effect=lambda: trace.append('spawned'))
            result = connector._run_bounded(argv, timeout=5000, input=b'reviewed script', on_spawn=callback)
            self.assertEqual(result.stdout, b'reviewed script')
            callback.assert_called_once_with()
            self.assert_reaped(children)

    def test_spawn_failure_keeps_original_exception_and_never_calls_spawn_callback(self):
        callback = mock.Mock()
        error = FileNotFoundError('missing fake Shell')
        with mock.patch.object(subprocess, 'Popen', side_effect=error):
            with self.assertRaises(FileNotFoundError) as caught:
                connector._run_bounded(['missing fake Shell'], timeout=5, idle_timeout=1, on_spawn=callback)
        self.assertIs(caught.exception, error)
        callback.assert_not_called()

    def test_spawn_callback_exception_cleans_up_already_started_process(self):
        script = 'import os\nreader, writer = os.pipe()\nos.read(reader, 1)\n'
        with self.child(script) as (argv, children, clock):
            callback = mock.Mock(side_effect=RuntimeError('post-spawn failure'))
            with self.assertRaisesRegex(RuntimeError, 'post-spawn failure'):
                connector._run_bounded(argv, timeout=5000, input=b'never sent', on_spawn=callback)
            callback.assert_called_once_with()
            self.assert_reaped(children)

    def test_legacy_timeout_retention_and_tail_budget_are_unchanged(self):
        script = ('import os\n'
                  'os.write(1, b"prefix\\nTAIL\\n")\n'
                  'os.write(2, b"stderr detail")\n'
                  'reader, writer = os.pipe()\n'
                  'os.read(reader, 1)\n')
        for tail in (None, ('TAIL', 0.1)):
            with self.subTest(tail=tail), self.child(script, scaled=False) as (argv, children, clock):
                kwargs = {} if tail is None else {'tail_timeout': tail}
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    connector._run_bounded(argv, timeout=0.5, **kwargs)
                self.assertIsNone(getattr(caught.exception, 'idle_timeout', None))
                self.assertEqual(caught.exception.timeout, 0.5 if tail is None else 0.1)
                self.assertEqual(caught.exception.output, None if tail is None else b'prefix\nTAIL\n')
                self.assertIsNone(caught.exception.stderr)
                self.assert_reaped(children)


if __name__ == '__main__':
    unittest.main()
