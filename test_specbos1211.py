"""No-hardware check of measure('raw') framing over a fake serial port.

Run: python -m unittest -v test_specbos1211
"""

import unittest
from unittest import mock

import numpy as np

from specbos1211 import Specbos1211, _SerialTransport

ACK: bytes = b"\x06"
BELL: bytes = b"\x07"


class FakeSerial:
    """Byte-level fake of the specbos 1211 (firmware 3.0.2) for FUNC 3 + FORM 9."""

    def __init__(self, n: int) -> None:
        self.n: int = n
        self.sent: list[str] = []
        self.rx: bytearray = bytearray()
        self.timeout: float = 1.0

    def write(self, data: bytes) -> None:
        cmd: str = data.decode("ascii").rstrip("\r")
        self.sent.append(cmd)
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


if __name__ == "__main__":
    unittest.main()
