"""Shared switches for the live Docker test classes.

Two environment variables steer every live test class
(test_connector_integration.py, test_schema_versions_live.py):

- ``MYFLAMES_LIVE_DOCKER=0``: skip with the reason "live Docker tests run
  in the integration workflow", even when Docker is available. The unit
  test matrix sets this so live tests run only in the integration
  workflow.
- ``MYFLAMES_REQUIRE_LIVE=1``: never skip. A missing Docker daemon or
  mysql client, an image pull failure, a container start failure or a
  readiness timeout fails the class from ``setUpClass``. The integration
  workflow sets this so it can never pass with zero live tests.
- Neither set: a missing Docker setup or a failed container start skips
  the class, as before.

``MYFLAMES_LIVE_DOCKER=0`` wins over ``MYFLAMES_REQUIRE_LIVE=1`` when both
are set.
"""
import os
import unittest

LIVE_DOCKER_SKIP_REASON = "live Docker tests run in the integration workflow"
NO_DOCKER_SKIP_REASON = "Docker or mysql client not available"


def live_docker_disabled():
    return os.environ.get("MYFLAMES_LIVE_DOCKER") == "0"


def live_required():
    return os.environ.get("MYFLAMES_REQUIRE_LIVE") == "1"


def class_skip_reason(have_docker):
    """Return the skip reason for a live class, or None when it must run."""
    if live_docker_disabled():
        return LIVE_DOCKER_SKIP_REASON
    if not have_docker and not live_required():
        return NO_DOCKER_SKIP_REASON
    return None


def maybe_skip(have_docker):
    """Class decorator: skip a live class unless it may run."""
    reason = class_skip_reason(have_docker)
    if reason is None:
        return lambda cls: cls
    return unittest.skip(reason)


def require_live_docker(problem):
    """setUpClass guard: fail a required live class when Docker is missing.

    *problem* is None when Docker and a mysql client are usable, else a
    description of what is missing.
    """
    if live_required() and problem is not None:
        raise AssertionError(
            "MYFLAMES_REQUIRE_LIVE=1 but the live environment is missing: " + problem
        )


def fail_or_skip(message):
    """Fail when live tests are required; skip otherwise."""
    if live_required():
        raise AssertionError(message)
    raise unittest.SkipTest(message)
