"""Unit tests for the MYFLAMES_LIVE_DOCKER / MYFLAMES_REQUIRE_LIVE switches.

Everything is monkeypatched: no Docker daemon, no mysql client and no
network is needed. Each test reloads the live modules from disk under a
temporary name (leaving sys.modules untouched) with a controlled
environment and a fake ``shutil.which`` / ``subprocess.run``, then checks
the class skip attributes or calls ``setUpClass`` directly.
"""
import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
CONNECTOR = os.path.join(TEST_DIR, "test_connector_integration.py")
SCHEMA = os.path.join(TEST_DIR, "test_schema_versions_live.py")

LIVE_REASON = "live Docker tests run in the integration workflow"
NO_DOCKER_REASON = "Docker or mysql client not available"

CONNECTOR_CLASSES = ("TestLiveMySQL84", "TestLiveMariaDB114")
SCHEMA_CLASSES = ("TestLiveSchemaVersionsMySQL84", "TestLiveSchemaVersionsMariaDB114")


@contextlib.contextmanager
def _env(live_docker=None, require_live=None):
    """Set exactly the given switch values, removing the rest."""
    with mock.patch.dict(os.environ):
        os.environ.pop("MYFLAMES_LIVE_DOCKER", None)
        os.environ.pop("MYFLAMES_REQUIRE_LIVE", None)
        if live_docker is not None:
            os.environ["MYFLAMES_LIVE_DOCKER"] = live_docker
        if require_live is not None:
            os.environ["MYFLAMES_REQUIRE_LIVE"] = require_live
        yield


def _fake_run(daemon_ok=True, run_fails=False):
    """A subprocess.run stand-in covering the docker calls in setUpClass."""
    def fake(cmd, **kwargs):
        result = mock.Mock()
        result.stdout = b""
        if cmd[:2] == ["docker", "info"]:
            result.returncode = 0 if daemon_ok else 1
            result.stderr = b"" if daemon_ok else b"Cannot connect to the Docker daemon"
        elif cmd[:2] == ["docker", "run"] and run_fails:
            result.returncode = 1
            result.stderr = b"Unable to find image locally: pull access denied"
        else:
            result.returncode = 0
            result.stderr = b""
        return result
    return fake


def _fake_which(available):
    return lambda binary: "/usr/bin/" + binary if available else None


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_connector(live_docker=None, require_live=None, docker=True,
                    daemon_ok=True, run_fails=False):
    with _env(live_docker, require_live):
        with mock.patch.object(shutil, "which", _fake_which(docker)):
            with mock.patch.object(subprocess, "run",
                                   _fake_run(daemon_ok=daemon_ok, run_fails=run_fails)):
                return _load_module("ci_fresh_connector", CONNECTOR)


def _load_schema(connector_module, live_docker=None, require_live=None):
    with _env(live_docker, require_live):
        with mock.patch.dict(sys.modules,
                             {"test_connector_integration": connector_module}):
            return _load_module("ci_fresh_schema", SCHEMA)


def _skip_why(cls):
    if not getattr(cls, "__unittest_skip__", False):
        return None
    return cls.__unittest_skip_why__


class TestLiveDockerOff(unittest.TestCase):
    """MYFLAMES_LIVE_DOCKER=0: every live class skips with the exact reason,
    whether or not Docker is available."""

    def test_connector_classes_skip_with_exact_reason(self):
        for docker in (True, False):
            with self.subTest(docker=docker):
                mod = _load_connector(live_docker="0", docker=docker)
                for name in CONNECTOR_CLASSES:
                    self.assertEqual(LIVE_REASON, _skip_why(getattr(mod, name)))

    def test_schema_classes_skip_with_exact_reason(self):
        for docker in (True, False):
            with self.subTest(docker=docker):
                connector = _load_connector(live_docker="0", docker=docker)
                mod = _load_schema(connector, live_docker="0")
                for name in SCHEMA_CLASSES:
                    self.assertEqual(LIVE_REASON, _skip_why(getattr(mod, name)))

    def test_live_docker_off_wins_over_require_live(self):
        mod = _load_connector(live_docker="0", require_live="1", docker=False)
        for name in CONNECTOR_CLASSES:
            self.assertEqual(LIVE_REASON, _skip_why(getattr(mod, name)))


class TestRequireLive(unittest.TestCase):
    """MYFLAMES_REQUIRE_LIVE=1: environment problems fail, never skip."""

    def test_classes_are_not_skipped(self):
        for docker in (True, False):
            with self.subTest(docker=docker):
                mod = _load_connector(require_live="1", docker=docker)
                for name in CONNECTOR_CLASSES:
                    self.assertIsNone(_skip_why(getattr(mod, name)))

    def test_missing_docker_fails_setup_with_the_reason(self):
        mod = _load_connector(require_live="1", docker=False)
        with _env(require_live="1"):
            for name in CONNECTOR_CLASSES:
                with self.subTest(name=name):
                    with self.assertRaises(AssertionError) as ctx:
                        getattr(mod, name).setUpClass()
                    self.assertIn("MYFLAMES_REQUIRE_LIVE=1", str(ctx.exception))
                    self.assertIn("docker", str(ctx.exception).lower())

    def test_missing_docker_fails_schema_setup(self):
        connector = _load_connector(require_live="1", docker=False)
        mod = _load_schema(connector, require_live="1")
        with _env(require_live="1"):
            with self.assertRaises(AssertionError) as ctx:
                mod.TestLiveSchemaVersionsMySQL84.setUpClass()
            self.assertIn("no docker binary on PATH", str(ctx.exception))

    def test_container_start_failure_fails_with_error_text(self):
        mod = _load_connector(require_live="1", run_fails=True)
        with _env(require_live="1"):
            with mock.patch.object(subprocess, "run", _fake_run(run_fails=True)):
                with self.assertRaises(AssertionError) as ctx:
                    mod.TestLiveMySQL84.setUpClass()
        self.assertIn("could not start", str(ctx.exception))
        self.assertIn("pull access denied", str(ctx.exception))

    def test_readiness_timeout_fails(self):
        mod = _load_connector(require_live="1")
        with _env(require_live="1"):
            with mock.patch.object(subprocess, "run", _fake_run()):
                with mock.patch.object(mod, "_wait_ready", lambda *a, **k: False):
                    with self.assertRaises(AssertionError) as ctx:
                        mod.TestLiveMySQL84.setUpClass()
        self.assertIn("did not become ready", str(ctx.exception))

    def test_readiness_timeout_fails_schema_class(self):
        connector = _load_connector(require_live="1")
        mod = _load_schema(connector, require_live="1")
        with _env(require_live="1"):
            with mock.patch.object(subprocess, "run", _fake_run()):
                with mock.patch.object(mod, "_wait_ready", lambda *a, **k: False):
                    with self.assertRaises(AssertionError) as ctx:
                        mod.TestLiveSchemaVersionsMariaDB114.setUpClass()
        self.assertIn("did not become ready", str(ctx.exception))


class TestDefaultMode(unittest.TestCase):
    """Neither switch set: today's skip behavior is unchanged."""

    def test_missing_docker_skips_with_legacy_reason(self):
        connector = _load_connector(docker=False)
        for name in CONNECTOR_CLASSES:
            self.assertEqual(NO_DOCKER_REASON, _skip_why(getattr(connector, name)))
        mod = _load_schema(connector)
        for name in SCHEMA_CLASSES:
            self.assertEqual(NO_DOCKER_REASON, _skip_why(getattr(mod, name)))

    def test_unreachable_daemon_skips(self):
        mod = _load_connector(daemon_ok=False)
        for name in CONNECTOR_CLASSES:
            self.assertEqual(NO_DOCKER_REASON, _skip_why(getattr(mod, name)))

    def test_available_docker_runs(self):
        mod = _load_connector()
        for name in CONNECTOR_CLASSES:
            self.assertIsNone(_skip_why(getattr(mod, name)))

    def test_container_start_failure_skips(self):
        mod = _load_connector(run_fails=True)
        with _env():
            with mock.patch.object(subprocess, "run", _fake_run(run_fails=True)):
                with self.assertRaises(unittest.SkipTest):
                    mod.TestLiveMySQL84.setUpClass()

    def test_readiness_timeout_skips(self):
        mod = _load_connector()
        with _env():
            with mock.patch.object(subprocess, "run", _fake_run()):
                with mock.patch.object(mod, "_wait_ready", lambda *a, **k: False):
                    with self.assertRaises(unittest.SkipTest):
                        mod.TestLiveMariaDB114.setUpClass()


if __name__ == "__main__":
    unittest.main()
