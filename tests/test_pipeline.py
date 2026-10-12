#!/usr/bin/env python3
"""Real Linux collector-to-analysis checks, without latency thresholds."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Running this file directly puts tests/, rather than the package root, first.
sys.path.insert(0, str(ROOT))
from wakeup_latency import ValidationError, load_run, summarize  # noqa: E402


BINARY = None


class CollectorAnalysisPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wakeup-pipeline-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "run.csv"

    def collect_complete(self, name="run.csv", samples=20):
        output = self.directory / name
        result = subprocess.run(
            [str(BINARY), "--interval-us", "1000", "--samples", str(samples),
             "--output", str(output)],
            cwd=self.directory, capture_output=True, text=True, timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return output

    def analysis_cli(self, command, *paths):
        return subprocess.run(
            [sys.executable, "-m", "wakeup_latency", command,
             *map(str, paths), "--json"],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )

    def assert_cli_matches_api(self, path, expected_exit):
        run = load_run(path)
        for command in ("validate", "summarize"):
            with self.subTest(command=command):
                result = self.analysis_cli(command, path)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                report, = json.loads(result.stdout)
                self.assertEqual(report["source"], str(path))
                self.assertEqual(report["classification"], run.classification)
                self.assertEqual(report["declared_status"], run.declared_status)
                self.assertEqual(report["requested_samples"], run.requested_samples)
                self.assertEqual(report["observed_samples"], len(run.samples))
                self.assertEqual(report["interval_ns"], run.interval_ns)
                self.assertEqual(report["observed_skipped_periods"],
                                 sum(row.skipped_periods for row in run.samples))
                if command == "summarize":
                    self.assertEqual(report["statistics"], asdict(summarize(run)))
        return run

    def test_complete_collector_output_api_and_cli(self):
        output = self.collect_complete()
        run = self.assert_cli_matches_api(output, 0)
        self.assertEqual(run.classification, "complete")
        self.assertEqual(run.requested_samples, 20)
        self.assertEqual(run.interval_ns, 1_000_000)
        self.assertEqual(len(run.samples), 20)
        statistics = summarize(run)
        self.assertEqual(statistics.count, 20)
        self.assertEqual(statistics.minimum_ns,
                         min(row.latency_ns for row in run.samples))
        self.assertEqual(statistics.maximum_ns,
                         max(row.latency_ns for row in run.samples))
        self.assertIsNotNone(statistics.sample_stddev_ns)

    def test_multiple_complete_files_are_reported_independently(self):
        first = self.collect_complete("first.csv", 3)
        second = self.collect_complete("second.csv", 5)
        result = self.analysis_cli("summarize", first, second)
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = json.loads(result.stdout)
        self.assertEqual([report["source"] for report in reports],
                         [str(first), str(second)])
        self.assertEqual([report["statistics"]["count"] for report in reports],
                         [3, 5])
        self.assertEqual([report["classification"] for report in reports],
                         ["complete", "complete"])

    def test_interrupted_collector_output_retains_classification(self):
        requested = 100_000
        for cancellation in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=cancellation):
                output = self.directory / f"interrupted-{int(cancellation)}.csv"
                process = subprocess.Popen(
                    [str(BINARY), "--interval-us", "1000", "--samples",
                     str(requested), "--output", str(output)],
                    cwd=self.directory, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
                try:
                    # Output creation precedes measurement. This limit only
                    # detects a hung process; it is not a latency requirement.
                    wait_limit = time.monotonic() + 10
                    while not output.exists() and process.poll() is None:
                        if time.monotonic() >= wait_limit:
                            self.fail("collector did not reach output setup")
                        time.sleep(0.005)
                    self.assertIsNone(process.poll(),
                                      "collector exited before cancellation")
                    time.sleep(0.05)
                    process.send_signal(cancellation)
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode,
                                     128 + int(cancellation), stderr)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                run = self.assert_cli_matches_api(output, 1)
                self.assertLess(len(run.samples), requested)
                if run.samples:
                    self.assertEqual(run.classification, "interrupted")
                    self.assertEqual(run.declared_status, "interrupted")
                    self.assertEqual(run.requested_samples, requested)
                else:
                    # Cancellation before the first observation is legal;
                    # schema v1 has no status metadata in a header-only file.
                    self.assertEqual(run.classification, "header-only")
                    self.assertIsNone(run.declared_status)

    def test_header_only_collector_prefix_has_unknown_status(self):
        complete = self.collect_complete(samples=1)
        header_only = self.directory / "header-only.csv"
        header_only.write_bytes(complete.read_bytes().splitlines(keepends=True)[0])
        run = self.assert_cli_matches_api(header_only, 1)
        self.assertEqual(run.classification, "header-only")
        self.assertIsNone(run.requested_samples)
        self.assertEqual(summarize(run).count, 0)
        self.assertIsNone(summarize(run).mean_ns)

    def test_complete_collector_prefix_is_truncated(self):
        complete = self.collect_complete(samples=3)
        truncated = self.directory / "truncated.csv"
        truncated.write_bytes(b"".join(
            complete.read_bytes().splitlines(keepends=True)[:-1]))
        run = self.assert_cli_matches_api(truncated, 1)
        self.assertEqual(run.classification, "truncated")
        self.assertEqual(run.declared_status, "complete")
        self.assertEqual(run.requested_samples, 3)
        self.assertEqual(summarize(run).count, 2)

    def test_torn_collector_record_is_rejected_without_salvage(self):
        complete = self.collect_complete(samples=3)
        malformed = self.directory / "torn.csv"
        records = complete.read_bytes().splitlines(keepends=True)
        malformed.write_bytes(b"".join(records[:-1]) + b"1,complete,3\n")
        with self.assertRaises(ValidationError) as caught:
            load_run(malformed)
        self.assertEqual(caught.exception.line, 4)
        for command in ("validate", "summarize"):
            with self.subTest(command=command):
                result = self.analysis_cli(command, malformed)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"{malformed}:4:", result.stderr)
                report, = json.loads(result.stdout)
                self.assertEqual(report["classification"], "invalid")
                self.assertIsNone(report["observed_samples"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    options, unittest_arguments = parser.parse_known_args()
    BINARY = options.binary.resolve()
    if not BINARY.is_file():
        parser.error(f"collector binary does not exist: {BINARY}")
    unittest.main(argv=[__file__, *unittest_arguments], verbosity=2)
