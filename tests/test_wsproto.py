"""Micro tests for the hand-rolled WebSocket layer."""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from link import wsproto  # noqa: E402
from link.util import free_port  # noqa: E402


class TestMasking(unittest.TestCase):
    def test_mask_roundtrip(self):
        for size in (0, 1, 3, 4, 5, 127, 4096):
            data = os.urandom(size)
            mask = os.urandom(4)
            once = wsproto._apply_mask(data, mask)
            self.assertEqual(len(once), size)
            self.assertEqual(wsproto._apply_mask(once, mask), data)

    def test_mask_matches_naive_implementation(self):
        data, mask = os.urandom(1000), os.urandom(4)
        naive = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.assertEqual(wsproto._apply_mask(data, mask), naive)

    def test_accept_key_matches_rfc6455_example(self):
        # The example key/accept pair from RFC 6455 section 1.3.
        self.assertEqual(
            wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class TestHandshakeAndFrames(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = free_port()
        self.received: list = []
        self.server_ready = asyncio.Event()

        async def handle(reader, writer):
            try:
                conn = await wsproto.server_handshake(reader, writer)
            except wsproto.WSError:
                return  # rejected handshake; the client-side assertion covers it
            while True:
                msg = await conn.recv()
                if msg is None:
                    break
                self.received.append(msg)
                await conn.send_text(f"echo:{msg}")
            await conn.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", self.port)

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_text_roundtrip(self):
        conn = await wsproto.ws_connect("127.0.0.1", self.port)
        await conn.send_text("ciao")
        self.assertEqual(await conn.recv(), "echo:ciao")
        await conn.close()
        self.assertEqual(self.received, ["ciao"])

    async def test_large_payload_uses_extended_length(self):
        conn = await wsproto.ws_connect("127.0.0.1", self.port)
        big = "x" * 200_000  # forces the 64-bit length path
        await conn.send_text(big)
        self.assertEqual(await conn.recv(), f"echo:{big}")
        await conn.close()

    async def test_unicode_survives(self):
        conn = await wsproto.ws_connect("127.0.0.1", self.port)
        payload = "perché — 日本語 — 🎉"
        await conn.send_text(payload)
        self.assertEqual(await conn.recv(), f"echo:{payload}")
        await conn.close()

    async def test_recv_returns_none_after_peer_close(self):
        conn = await wsproto.ws_connect("127.0.0.1", self.port)
        await conn.send_text("bye")
        await conn.recv()
        await conn.close()
        conn2 = await wsproto.ws_connect("127.0.0.1", self.port)
        await conn2.close()
        self.assertTrue(conn2.closed)

    async def test_remote_ip_is_exposed(self):
        conn = await wsproto.ws_connect("127.0.0.1", self.port)
        self.assertEqual(conn.remote_ip, "127.0.0.1")
        self.assertEqual(conn.remote_port, self.port)
        await conn.close()

    async def test_bad_path_is_rejected(self):
        with self.assertRaises(wsproto.WSError):
            await wsproto.ws_connect("127.0.0.1", self.port, path="/nope")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMaskingAgreesWithItselfAtEverySize(unittest.TestCase):
    """`_apply_mask` was one big-int XOR, which peaked at 5.2x the payload:
    1334 KiB for a 256 KiB frame, and at the relay's connection ceiling that is
    ~340 MiB of transient on a box sized for far less. It works in 64 KiB
    chunks now.

    Chunking a mask is easy to get subtly wrong -- the mask repeats every four
    bytes, so a chunk boundary that is not a multiple of four shifts the phase
    for everything after it and corrupts the tail of large frames only. That is
    the kind of defect that passes every small test and fails on real traffic,
    so the boundaries are the point of this.
    """

    @staticmethod
    def one_shot(data: bytes, mask: bytes) -> bytes:
        """The original implementation, kept here as the oracle."""
        if not mask or not data:
            return data
        rep = mask * (len(data) // 4) + mask[: len(data) % 4]
        return (int.from_bytes(data, "big")
                ^ int.from_bytes(rep, "big")).to_bytes(len(data), "big")

    def sizes(self):
        chunk = wsproto._MASK_CHUNK
        return [0, 1, 2, 3, 4, 5, 125, 126, 127, 128,
                chunk - 1, chunk, chunk + 1, chunk + 3,
                2 * chunk, 2 * chunk + 7, 3 * chunk - 1, 256 * 1024]

    def test_it_agrees_with_the_one_shot_form(self):
        for n in self.sizes():
            data, mask = os.urandom(n), os.urandom(4)
            self.assertEqual(wsproto._apply_mask(data, mask),
                             self.one_shot(data, mask), f"length {n}")

    def test_masking_twice_returns_the_original(self):
        for n in self.sizes():
            data, mask = os.urandom(n), os.urandom(4)
            self.assertEqual(
                wsproto._apply_mask(wsproto._apply_mask(data, mask), mask),
                data, f"length {n}")

    def test_the_chunk_keeps_the_mask_in_phase(self):
        """A chunk that is not a multiple of four is the whole bug."""
        self.assertEqual(wsproto._MASK_CHUNK % 4, 0)
