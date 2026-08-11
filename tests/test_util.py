"""Local IP discovery must never block on a slow resolver.

The daemon calls `primary_ip()` during startup, just after its control port
starts listening. Anything that blocks there produces the worst failure shape
this codebase has: the port accepts connections, so the daemon looks alive,
but nothing is ever answered and nothing is ever logged.

That is not hypothetical. It shipped, and it cost a CI investigation that
concluded the opposite -- that macOS was merely slow -- because the only
evidence a wedged daemon leaves is an empty log.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# This directory too, for the `timing` helper beside this file. `unittest
# discover -s tests` adds it and nothing else does, so without this line
# `python -m unittest tests.test_util` -- the obvious way to run one module --
# dies on `No module named 'timing'` while the full suite is green. A test that
# only runs one way is one nobody reaches for.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from link import util  # noqa: E402
from timing import budget  # noqa: E402


class HostnameLookupIsBounded(unittest.TestCase):
    def setUp(self) -> None:
        util._hostname_lookup_is_slow = False
        self.release = threading.Event()

    def tearDown(self) -> None:
        # Let any parked resolver thread go before the next test runs.
        self.release.set()
        util._hostname_lookup_is_slow = False

    def _blocking_getaddrinfo(self, *_a, **_kw):
        self.release.wait(30)
        return []

    def test_a_wedged_resolver_does_not_stall_the_caller(self):
        with mock.patch.object(socket, "getaddrinfo", self._blocking_getaddrinfo):
            start = time.monotonic()
            ips = util.local_ips(timeout=0.25)
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, budget(3.0),
                        f"local_ips blocked for {elapsed:.1f}s on a wedged resolver")
        self.assertTrue(ips, "should still report an address from the UDP probe")

    def test_the_daemon_startup_path_is_bounded_too(self):
        """`primary_ip` is what daemon.run() actually calls."""
        with mock.patch.object(socket, "getaddrinfo", self._blocking_getaddrinfo):
            start = time.monotonic()
            ip = util.primary_ip()
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, budget(3.0),
                        f"primary_ip blocked for {elapsed:.1f}s during startup")
        self.assertTrue(ip)

    def test_the_timeout_is_paid_once_not_per_call(self):
        """A slow resolver is a property of the machine, not of the moment."""
        with mock.patch.object(socket, "getaddrinfo", self._blocking_getaddrinfo):
            util.local_ips(timeout=0.25)
            start = time.monotonic()
            for _ in range(5):
                util.local_ips(timeout=0.25)
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, budget(0.25),
                        "later calls paid the hostname timeout again")

    def test_a_healthy_resolver_still_contributes_its_addresses(self):
        def fast_getaddrinfo(*_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.77", 0))]

        with mock.patch.object(socket, "getaddrinfo", fast_getaddrinfo):
            ips = util.local_ips(timeout=5.0)

        self.assertIn("192.0.2.77", ips)

    def test_loopback_and_duplicates_are_still_filtered(self):
        def loopback_only(*_a, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        with mock.patch.object(socket, "getaddrinfo", loopback_only):
            ips = util.local_ips(timeout=5.0)

        self.assertEqual(len(ips), len(set(ips)))
        self.assertNotIn("127.0.0.1", ips[1:])


if __name__ == "__main__":
    unittest.main()
