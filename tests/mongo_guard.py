"""The rule that keeps tests off the real MongoDB data and says clearly what to do when the container is not running.

MongoDB is the only store, so any test may open it. What a test may open is limited by the data layer itself (``mongo.check_name``: under pytest only ``aivf_test_*``, and conftest gives every test its own
name before anything connects). This module only decides what happens when the container is down:

* by default the whole session stops at the start with one message that names the container and how to start it (``conftest.pytest_sessionstart``), because a run in which a third of the tests are
  silently skipped would be green and mean nothing;
* with ``ZTSA_TEST_SKIP_MONGO=1`` the session runs, and each test that tries to open MongoDB is skipped with that reason; the final summary counts them.

``install`` replaces ``pymongo.MongoClient`` for the length of one test so that either rule applies at the moment a test makes a client, and records whether it did.
"""
from __future__ import annotations

from dataclasses import dataclass

SKIP_REASON = "MongoDB is not reachable and ZTSA_TEST_SKIP_MONGO=1: this test needs the Atlas Local container (docker start atlas-final)"
ABORT_MESSAGE = ("MongoDB is not reachable at localhost:27017. The tests run against the Atlas Local container: start Docker Desktop and run `docker start atlas-final`, "
                 "or set ZTSA_TEST_SKIP_MONGO=1 to run without it (every test that needs the database is then skipped and counted in the summary).")


@dataclass
class Guard:
    skip: bool = False                 # the container is down and skipping was asked for
    opened: bool = False               # this test made a client (so it is the test's to close, and its database is the test's to drop)


def install(monkeypatch, skip: bool = False) -> Guard:
    import pymongo

    guard = Guard(skip=skip)
    real = getattr(pymongo.MongoClient, "_aivf_real", pymongo.MongoClient)      # a second install replaces the first guard, it does not wrap it

    class GuardedClient(real):                                     # type: ignore[misc, valid-type]
        _aivf_real = real

        def __init__(self, *args, **kwargs):
            if guard.skip:
                import pytest
                pytest.skip(SKIP_REASON)                           # raises Skipped, which no `except Exception` in the code under test can swallow
            guard.opened = True
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pymongo, "MongoClient", GuardedClient)
    return guard
