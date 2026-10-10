"""No-hardware checks of the serial protocol over a fake serial port.

Run: python -m unittest -v test_specbos1211
"""

import unittest
from unittest import mock

import numpy as np

from specbos1211 import JetiError, Specbos1211, _SerialTransport

ACK: bytes = b"\x06"
BELL: bytes = b"\x07"


class FakeSerial:
    """Byte-level fake of the specbos 1211 (firmware 3.0.2) for FUNC 3 + FORM 9."""

    def __init__(self, n: int) -> None:
        self.n: int = n
        self.sent: list[str] = []
        self.rx: bytearray = bytearray()
        self.timeout: float = 1.0
        self.reply: bytes | None = None  # if set, the answer to every command

    def write(self, data: bytes) -> None:
        cmd: str = data.decode("ascii").rstrip("\r")
        self.sent.append(cmd)
        if self.reply is not None:
            self.rx += self.reply
            return
        if cmd == "*MEAS":
            # One float per point, each ends in a single \r; no \r\r terminator.
            lines: bytes = b"".join(b"%.2f\r" % (i - 3.5) for i in range(self.n))
            self.rx += ACK + BELL + lines
        else:
            self.rx += ACK

    def read(self, size: int) -> bytes:
        out: bytes = bytes(self.rx[:size])
        del self.rx[:size]
        return out

    @property
    def in_waiting(self) -> int:
        return len(self.rx)


def _open(fake: FakeSerial) -> Specbos1211:
    with mock.patch("specbos1211.serial.Serial", return_value=fake):
        return Specbos1211(_SerialTransport("fake", 115200, 1.0))


class RawMeasureTest(unittest.TestCase):
    def test_raw_selects_func3_and_reads_point_count(self) -> None:
        fake: FakeSerial = FakeSerial(401)
        with mock.patch("specbos1211.serial.Serial", return_value=fake):
            dev: Specbos1211 = Specbos1211(_SerialTransport("fake", 921600, 1.0))
        dev.configure(wbeg=380, wend=780, step=1)
        fake.sent.clear()

        counts: np.ndarray = dev.measure("raw")

        self.assertEqual(
            fake.sent, ["*CONF:FUNC 3", "*CONF:FORM 9", "*MEAS", "*CONF:FORM 4"]
        )
        self.assertEqual(counts.shape, (401,))
        self.assertEqual(counts[0], -3.5)  # dark-subtracted, may be negative
        self.assertEqual(counts[-1], 396.5)
        self.assertEqual(fake.rx, b"")  # nothing left unread


class StrayBytesTest(unittest.TestCase):
    def test_stray_text_is_flushed_and_logged(self) -> None:
        fake: FakeSerial = FakeSerial(401)
        dev: Specbos1211 = _open(fake)
        fake.rx += b"I-stray-text\r"  # arrived while idle
        with self.assertLogs("specbos1211", "WARNING") as logs:
            dev.configure(tint=0.0)
        self.assertIn("I-stray-text", logs.output[0])
        self.assertIn("*CONF:EXPO 1", logs.output[0])
        self.assertEqual(fake.rx, b"")

    def test_enq_before_ack_is_skipped(self) -> None:
        fake: FakeSerial = FakeSerial(401)
        dev: Specbos1211 = _open(fake)
        fake.reply = b"\x05\x06"
        dev.configure(tint=0.0)
        self.assertEqual(fake.rx, b"")

    def test_unexpected_byte_names_command_and_pending(self) -> None:
        fake: FakeSerial = FakeSerial(401)
        dev: Specbos1211 = _open(fake)
        fake.reply = b"Ixyz"
        with self.assertRaises(JetiError) as cm:
            dev.configure(tint=0.0)
        self.assertEqual(
            str(cm.exception),
            "unexpected response byte 0x49 to '*CONF:EXPO 1'; pending: b'Ixyz'",
        )


class LastTintTest(unittest.TestCase):
    def _reply(self, reply: bytes) -> float | None:
        fake: FakeSerial = FakeSerial(401)
        dev: Specbos1211 = _open(fake)
        fake.reply = reply
        return dev.last_tint_ms()

    def test_parses_previous_tint(self) -> None:
        reply: bytes = b"Previous tint: 432.5 ms\rConfigured tint: 0 ms\r"
        self.assertEqual(self._reply(reply), 432.5)

    def test_unparseable_or_silent_gives_none(self) -> None:
        self.assertIsNone(self._reply(b"garbage\r"))
        self.assertIsNone(self._reply(b""))  # times out, does not hang


if __name__ == "__main__":
    unittest.main()
