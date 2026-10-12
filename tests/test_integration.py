#!/usr/bin/env python3
"""CLI/CSV tests for the C collector; no latency-performance assumptions."""

import argparse
import csv
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


FIELDS = [
    "schema_version", "run_status", "requested_samples", "sample", "interval_ns",
    "deadline_ns", "observed_ns", "latency_ns", "skipped_periods",
]
BINARY = None


class CollectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wakeup-latency-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / "run.csv"

    def run_collector(self, *arguments, timeout=10):
        return subprocess.run(
            [str(BINARY), *arguments], cwd=self.directory, capture_output=True,
            text=True, timeout=timeout, check=False,
        )

    def read_rows(self, output=None):
        with (output or self.output).open(newline="", encoding="ascii") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, FIELDS)
            rows = list(reader)
        for row in rows:
            self.assertEqual(set(row), set(FIELDS))
            self.assertTrue(all(value is not None for value in row.values()))
        return rows

    def assert_rows_valid(self, rows, requested, interval_ns, status):
        previous_deadline = 0
        previous_observed = 0
        for index, row in enumerate(rows, start=1):
            self.assertEqual(row["run_status"], status)
            numeric = {name: int(value) for name, value in row.items()
                       if name != "run_status"}
            self.assertTrue(all(value >= 0 for value in numeric.values()))
            self.assertEqual(numeric["schema_version"], 1)
            self.assertEqual(numeric["requested_samples"], requested)
            self.assertEqual(numeric["sample"], index)
            self.assertEqual(numeric["interval_ns"], interval_ns)
            deadline = numeric["deadline_ns"]
            observed = numeric["observed_ns"]
            self.assertEqual(deadline - previous_deadline,
                             (numeric["skipped_periods"] + 1) * interval_ns)
            self.assertGreaterEqual(observed, deadline)
            self.assertGreaterEqual(observed, previous_observed)
            self.assertEqual(numeric["latency_ns"], observed - deadline)
            previous_deadline = deadline
            previous_observed = observed

    def test_short_measurement_schema_and_arithmetic(self):
        result = self.run_collector("--interval-us", "1000", "--samples", "20",
                                    "--output", str(self.output))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.read_rows()
        self.assertEqual(len(rows), 20)
        self.assert_rows_valid(rows, 20, 1_000_000, "complete")

    def test_default_output_path(self):
        result = self.run_collector("--interval-us", "1000", "--samples", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.read_rows(self.directory / "latency.csv")
        self.assertEqual(len(rows), 3)
        self.assert_rows_valid(rows, 3, 1_000_000, "complete")

    def test_help(self):
        result = self.run_collector("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--interval-us", result.stdout)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_arguments_and_ranges(self):
        cases = [
            ["--unknown"], ["positional"], ["--samples"], ["--output"],
            ["--samples", "0"], ["--samples", "-1"],
            ["--samples", "+1"], ["--samples", "1.5"],
            ["--samples", " 1"], ["--samples", "1 "],
            ["--samples", "\u0661"], ["--samples", "18446744073709551616"],
            ["--samples", "8388609"], ["--interval-us", "0"],
            ["--interval-us", "1x"], ["--interval-us", "9223372036854776"],
            ["--samples", "2", "--interval-us", "4611686018427388"],
            ["--samples", "1", "--samples", "2"],
            ["--overwrite", "--overwrite"], ["--help", "--samples", "1"],
            ["--samples=1"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_collector(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(result.stderr)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_existing_output_preserved_without_overwrite(self):
        original = b"previous results\n"
        self.output.write_bytes(original)
        result = self.run_collector("--samples", "1", "--output", str(self.output))
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(self.output.read_bytes(), original)

    def test_explicit_overwrite(self):
        self.output.write_text("previous results\n", encoding="ascii")
        result = self.run_collector("--interval-us", "1000", "--samples", "2",
                                    "--output", str(self.output), "--overwrite")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assert_rows_valid(rows, 2, 1_000_000, "complete")

    def test_symlink_output_rejected(self):
        target = self.directory / "preserve.txt"
        original = b"preserve this target\n"
        target.write_bytes(original)
        self.output.symlink_to(target)
        result = self.run_collector("--samples", "1", "--output", str(self.output),
                                    "--overwrite")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.output.is_symlink())

    def test_parent_directory_symlink_allowed(self):
        target_directory = self.directory / "actual"
        target_directory.mkdir()
        alias = self.directory / "alias"
        alias.symlink_to(target_directory, target_is_directory=True)
        output = alias / "run.csv"
        result = self.run_collector("--interval-us", "1000", "--samples", "2",
                                    "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.read_rows(target_directory / "run.csv")
        self.assertEqual(len(rows), 2)
        self.assert_rows_valid(rows, 2, 1_000_000, "complete")

    def test_invalid_output_destinations(self):
        for destination in [self.directory, self.directory / "missing" / "run.csv"]:
            with self.subTest(destination=destination):
                result = self.run_collector("--samples", "1", "--output",
                                            str(destination))
                self.assertEqual(result.returncode, 5, result.stderr)
                self.assertTrue(result.stderr)

    @unittest.skipUnless(Path("/dev/full").exists(), "/dev/full unavailable")
    def test_device_output_rejected(self):
        result = self.run_collector("--interval-us", "1000", "--samples", "2",
                                    "--output", "/dev/full", "--overwrite")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("regular file", result.stderr)

    def test_fifo_without_reader_rejected_without_hanging(self):
        fifo = self.directory / "no-reader.fifo"
        os.mkfifo(fifo)
        result = self.run_collector("--samples", "1", "--output", str(fifo),
                                    "--overwrite")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertTrue(result.stderr)

    def test_fifo_with_reader_rejected(self):
        fifo = self.directory / "reader.fifo"
        os.mkfifo(fifo)
        descriptor = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            result = self.run_collector("--samples", "1", "--output", str(fifo),
                                        "--overwrite")
        finally:
            os.close(descriptor)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("regular file", result.stderr)

    def test_interrupted_runs(self):
        requested = 100_000
        for cancellation in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=cancellation):
                output = self.directory / f"signal-{int(cancellation)}.csv"
                process = subprocess.Popen(
                    [str(BINARY), "--interval-us", "1000", "--samples",
                     str(requested), "--output", str(output)],
                    cwd=self.directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    # Synchronize with output setup, then allow some measurement work.
                    # The deadline below is a hang detector, not a latency threshold.
                    wait_limit = time.monotonic() + 10
                    while not output.exists() and process.poll() is None:
                        if time.monotonic() >= wait_limit:
                            self.fail("collector did not reach output setup")
                        time.sleep(0.005)
                    self.assertIsNone(process.poll(), "collector exited before cancellation")
                    time.sleep(0.05)
                    process.send_signal(cancellation)
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + int(cancellation), stderr)
                    rows = self.read_rows(output)
                    self.assertLess(len(rows), requested)
                    # Zero completed rows are legal if cancellation precedes observation.
                    self.assert_rows_valid(rows, requested, 1_000_000, "interrupted")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    options, unittest_arguments = parser.parse_known_args()
    BINARY = options.binary.resolve()
    if not BINARY.is_file():
        parser.error(f"collector binary does not exist: {BINARY}")
    unittest.main(argv=[__file__, *unittest_arguments], verbosity=2)
