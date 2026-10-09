"""No-hardware checks for the GUI's math, CSV writer and worker.

Run: python -m unittest -v test_specbos1211_gui
"""

import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path

import numpy as np

from specbos1211_gui import (
    AUTO_TIMEOUT_S,
    DemoSpecbos,
    Spectrum,
    _measure,
    analyse,
    append_csv,
)


class GuiTest(unittest.TestCase):
    def test_analyse_demo_led(self) -> None:
        dev = DemoSpecbos()
        dev.configure()
        wl: np.ndarray = np.asarray(dev.wavelengths)
        luminance, radiance, (y, u, v) = analyse(wl, dev.measure())
        self.assertAlmostEqual(luminance, 99.5, delta=0.5)  # cd/m2
        self.assertAlmostEqual(radiance, 0.299, delta=0.002)  # W sr-1 m-2
        self.assertEqual(y, luminance)
        self.assertAlmostEqual(u, 0.249, delta=0.002)  # warm white, ~3000 K
        self.assertAlmostEqual(v, 0.526, delta=0.002)

    def test_analyse_step_5nm_matches_1nm(self) -> None:
        dev = DemoSpecbos()
        dev.configure(step=1)
        fine: float = analyse(np.asarray(dev.wavelengths), dev.measure())[0]
        dev.configure(step=5)
        coarse: float = analyse(np.asarray(dev.wavelengths), dev.measure())[0]
        self.assertAlmostEqual(coarse, fine, delta=0.01 * fine)

    def test_append_csv_and_header_guard(self) -> None:
        wl: np.ndarray = np.arange(380.0, 781.0)
        with tempfile.TemporaryDirectory() as d:
            path: Path = Path(d) / "out.csv"
            append_csv(path, wl, np.ones(wl.size), 0.0, 1.0, 2.0, (1.0, 0.2, 0.5))
            append_csv(path, wl, np.ones(wl.size), 50.0, 1.0, 2.0, (1.0, 0.2, 0.5))
            lines: list[str] = path.read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(len(lines[0].split(",")), 7 + wl.size)
            self.assertEqual(lines[1].split(",")[1], "auto")
            self.assertEqual(lines[2].split(",")[1], "50")
            with self.assertRaises(ValueError):  # other wavelength grid
                append_csv(path, wl[:10], np.ones(10), 0.0, 1.0, 2.0, (1.0, 0.2, 0.5))

    def test_worker_sets_timeout_and_result(self) -> None:
        dev = DemoSpecbos()
        job: Future[Spectrum] = Future()
        _measure(job, dev, 0.0)
        wl, spectrum = job.result(timeout=0)
        self.assertEqual(dev.timeout, AUTO_TIMEOUT_S)
        self.assertEqual(wl.shape, spectrum.shape)
        _measure(Future(), dev, 2000.0)
        self.assertEqual(dev.timeout, 32.0)

    def test_worker_hands_errors_to_future(self) -> None:
        class Short(DemoSpecbos):
            def measure(self, quantity: str = "sprad") -> np.ndarray:
                return super().measure()[:-1]  # truncated transfer

        job: Future[Spectrum] = Future()
        _measure(job, Short(), 0.0)
        with self.assertRaisesRegex(Exception, "wavelength grid"):
            job.result(timeout=0)


if __name__ == "__main__":
    unittest.main()
