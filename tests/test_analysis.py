"""Deterministic tests for CSV validation, statistics, and the analysis CLI."""

import csv
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from wakeup_latency import ValidationError, load_run, summarize


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "schema-v1"
HEADER = [
    "schema_version", "run_status", "requested_samples", "sample", "interval_ns",
    "deadline_ns", "observed_ns", "latency_ns", "skipped_periods",
]
INT64_MAX = (1 << 63) - 1
UINT64_MAX = (1 << 64) - 1


def record(*, status="complete", requested=1, sample=1, interval=1000,
           deadline=1000, observed=1010, latency=10, skipped=0):
    return [1, status, requested, sample, interval, deadline, observed, latency,
            skipped]


class TemporaryCSVTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wakeup-analysis-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "synthetic.csv"

    def write_rows(self, rows, header=HEADER, *, newline="\n"):
        with self.path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator=newline)
            writer.writerow(header)
            writer.writerows(rows)
        return self.path

    def assert_invalid(self, path, line=None):
        with self.assertRaises(ValidationError) as caught:
            load_run(path)
        error = caught.exception
        self.assertEqual(error.source, str(path))
        self.assertTrue(error.message)
        self.assertIn(str(path), str(error))
        if line is not None:
            self.assertEqual(error.line, line)
            self.assertIn(f":{line}", str(error))
        return error


class FixtureTests(TemporaryCSVTests):
    def test_complete_fixture(self):
        run = load_run(FIXTURES / "complete.csv")
        self.assertEqual(run.classification, "complete")
        self.assertEqual(run.declared_status, "complete")
        self.assertEqual(run.requested_samples, 5)
        self.assertEqual(run.interval_ns, 1000)
        self.assertEqual(len(run.samples), 5)
        self.assertIsInstance(run.samples, tuple)
        self.assertIsNone(run.diagnostic)
        self.assertIsNone(run.diagnostic_line)
        self.assertEqual([sample.sample for sample in run.samples], [1, 2, 3, 4, 5])
        self.assertEqual([sample.deadline_ns for sample in run.samples],
                         [1000, 2000, 5000, 6000, 8000])
        self.assertEqual([sample.latency_ns for sample in run.samples],
                         [0, 10, 20, 30, 100])
        self.assertEqual([sample.skipped_periods for sample in run.samples],
                         [0, 0, 2, 0, 1])

    def test_partial_statuses_are_distinct(self):
        for name, status, count, requested in [
            ("interrupted.csv", "interrupted", 2, 5),
            ("error.csv", "error", 2, 5),
            ("interrupted-full-count.csv", "interrupted", 2, 2),
            ("error-full-count.csv", "error", 2, 2),
        ]:
            with self.subTest(name=name):
                run = load_run(FIXTURES / name)
                self.assertEqual(run.classification, status)
                self.assertEqual(run.declared_status, status)
                self.assertEqual(run.requested_samples, requested)
                self.assertEqual(len(run.samples), count)

    def test_valid_complete_prefix_is_classified_truncated(self):
        run = load_run(FIXTURES / "truncated.csv")
        self.assertEqual(run.classification, "truncated")
        self.assertEqual(run.declared_status, "complete")
        self.assertEqual(run.requested_samples, 5)
        self.assertEqual(len(run.samples), 2)
        self.assertTrue(run.diagnostic)
        self.assertIsInstance(run.diagnostic_line, int)
        self.assertGreaterEqual(run.diagnostic_line, 3)

    def test_header_only_has_no_invented_metadata(self):
        run = load_run(FIXTURES / "header-only.csv")
        self.assertEqual(run.classification, "header-only")
        self.assertEqual(run.samples, ())
        self.assertIsNone(run.declared_status)
        self.assertIsNone(run.requested_samples)
        self.assertIsNone(run.interval_ns)
        self.assertTrue(run.diagnostic)

    def test_final_record_without_newline_is_valid(self):
        path = FIXTURES / "complete-no-final-newline.csv"
        self.assertFalse(path.read_bytes().endswith(b"\n"))
        self.assertEqual(load_run(path).classification, "complete")

    def test_first_deadline_can_include_skips(self):
        run = load_run(FIXTURES / "initial-skips.csv")
        self.assertEqual(run.classification, "complete")
        self.assertEqual(run.samples[0].deadline_ns, 4000)
        self.assertEqual(run.samples[0].skipped_periods, 3)

    def test_malformed_fixtures(self):
        cases = [
            ("invalid-header.csv", 1), ("duplicate-header.csv", 3),
            ("missing-value.csv", 2), ("non-integer.csv", 2),
            ("negative-latency.csv", 2), ("inconsistent-latency.csv", 2),
            ("invalid-skips.csv", 3), ("sample-gap.csv", 3),
            ("mixed-status.csv", 3), ("unsupported-version.csv", 2),
            ("unknown-status.csv", 2), ("torn-row.csv", 3),
            ("blank-row.csv", 3), ("extra-column.csv", 2),
            ("deadline-not-future.csv", 3),
        ]
        for name, line in cases:
            with self.subTest(name=name):
                self.assert_invalid(FIXTURES / name, line)


class ValidationTests(TemporaryCSVTests):
    def test_exact_header_is_required(self):
        headers = [
            HEADER[:-1], HEADER + ["extra"], list(reversed(HEADER)),
            [" schema_version"] + HEADER[1:],
            HEADER[:-1] + ["latency_ns"],
            ["Schema_version"] + HEADER[1:],
            ["\ufeffschema_version"] + HEADER[1:],
        ]
        for header in headers:
            with self.subTest(header=header):
                self.assert_invalid(self.write_rows([record()], header), 1)

    def test_csv_quoting_and_crlf_are_supported(self):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(HEADER)
        writer.writerow(record())
        self.path.write_bytes(buffer.getvalue().encode("ascii"))
        self.assertEqual(load_run(self.path).classification, "complete")

    def test_empty_input_is_invalid(self):
        self.path.write_bytes(b"")
        self.assert_invalid(self.path, 1)

    def test_missing_and_extra_fields_are_invalid(self):
        for row in [record()[:-1], record() + ["extra"], [], [""]]:
            with self.subTest(row=row):
                self.assert_invalid(self.write_rows([row]), 2)

    def test_numeric_fields_require_ascii_unsigned_decimal(self):
        malformed = ["", "-1", "+1", "1.0", "1e3", " 1", "1 ",
                     "\t1", "1_000", "\u0661", "\uff11", "\x001"]
        for index in [0, 2, 3, 4, 5, 6, 7, 8]:
            for value in malformed:
                with self.subTest(field=HEADER[index], value=value):
                    row = record()
                    row[index] = value
                    # Some csv.writer versions reject NULs before validation.
                    # These fields contain no CSV delimiters; write them raw.
                    self.path.write_bytes((",".join(HEADER) + "\n" +
                                           ",".join(map(str, row)) + "\n").encode("utf-8"))
                    self.assert_invalid(self.path, 2)

    def test_leading_zeroes_do_not_change_numeric_meaning(self):
        row = ["01", "complete", "001", "01", "01000", "01000", "01010",
               "010", "000"]
        run = load_run(self.write_rows([row]))
        self.assertEqual(run.classification, "complete")
        self.assertEqual(run.samples[0].latency_ns, 10)

    def test_positive_counts_samples_and_interval_required(self):
        for index in [2, 3, 4]:
            with self.subTest(field=HEADER[index]):
                row = record()
                row[index] = 0
                self.assert_invalid(self.write_rows([row]), 2)

    def test_integer_ranges_checked(self):
        for index in [2, 3, 8]:
            with self.subTest(field=HEADER[index]):
                row = record()
                row[index] = UINT64_MAX + 1
                self.assert_invalid(self.write_rows([row]), 2)
        for index in [4, 5, 6, 7]:
            with self.subTest(field=HEADER[index]):
                row = record()
                row[index] = INT64_MAX + 1
                self.assert_invalid(self.write_rows([row]), 2)

    def test_extremely_long_integer_is_a_validation_error(self):
        row = record()
        row[2] = "9" * 5000
        self.assert_invalid(self.write_rows([row]), 2)

    def test_schema_does_not_impose_current_producer_resource_limits(self):
        row = record(status="interrupted", requested=UINT64_MAX,
                     interval=INT64_MAX, deadline=INT64_MAX,
                     observed=INT64_MAX, latency=0)
        run = load_run(self.write_rows([row]))
        self.assertEqual(run.classification, "interrupted")
        self.assertEqual(run.requested_samples, UINT64_MAX)
        self.assertEqual(run.interval_ns, INT64_MAX)

    def test_large_skips_are_checked_without_wraparound(self):
        row = record(interval=1, deadline=INT64_MAX, observed=INT64_MAX,
                     latency=0, skipped=INT64_MAX - 1)
        run = load_run(self.write_rows([row]))
        self.assertEqual(run.samples[0].skipped_periods, INT64_MAX - 1)
        row[8] = UINT64_MAX
        self.assert_invalid(self.write_rows([row]), 2)

    def test_repeated_metadata_must_match(self):
        rows = [record(requested=2), record(requested=2, sample=2,
                                          deadline=2000, observed=2010)]
        for index, value in [(0, 2), (1, "error"), (2, 3), (4, 2000)]:
            with self.subTest(field=HEADER[index]):
                changed = [row.copy() for row in rows]
                changed[1][index] = value
                self.assert_invalid(self.write_rows(changed), 3)

    def test_sample_order_must_start_at_one_without_duplicates_or_gaps(self):
        for first in [0, 2]:
            with self.subTest(first=first):
                self.assert_invalid(self.write_rows([record(sample=first)]), 2)
        for second in [1, 3]:
            with self.subTest(second=second):
                rows = [record(requested=2),
                        record(requested=2, sample=second, deadline=2000,
                               observed=2010)]
                self.assert_invalid(self.write_rows(rows), 3)

    def test_observation_and_latency_equation(self):
        for row in [record(observed=999, latency=0), record(latency=11)]:
            with self.subTest(row=row):
                self.assert_invalid(self.write_rows([row]), 2)

    def test_observation_cannot_go_backwards(self):
        rows = [record(requested=2, observed=2500, latency=1500),
                record(requested=2, sample=2, deadline=2000, observed=2100,
                       latency=100)]
        self.assert_invalid(self.write_rows(rows), 3)

    def test_next_deadline_must_be_strictly_after_previous_observation(self):
        for observed in [2000, 2001]:
            with self.subTest(observed=observed):
                rows = [record(requested=2, observed=observed,
                               latency=observed - 1000),
                        record(requested=2, sample=2, deadline=2000,
                               observed=2100, latency=100)]
                self.assert_invalid(self.write_rows(rows), 3)

    def test_deadline_grid_and_skips_must_match(self):
        for row in [record(deadline=999, observed=1009),
                    record(deadline=2000, observed=2010, skipped=0),
                    record(deadline=1000, observed=1010, skipped=1)]:
            with self.subTest(row=row):
                self.assert_invalid(self.write_rows([row]), 2)

    def test_more_samples_than_requested_invalid_for_every_status(self):
        for status in ["complete", "interrupted", "error"]:
            with self.subTest(status=status):
                rows = [record(status=status),
                        record(status=status, sample=2, deadline=2000,
                               observed=2010)]
                self.assert_invalid(self.write_rows(rows), 3)

    def test_unterminated_quote_and_invalid_encoding_are_validation_errors(self):
        self.path.write_text(",".join(HEADER) + '\n1,complete,1,1,1000,1000,1010,10,"0',
                             encoding="ascii")
        self.assert_invalid(self.path, 2)
        self.path.write_bytes(",".join(HEADER).encode("ascii") + b"\n\xff\n")
        self.assert_invalid(self.path, 2)

    def test_multiline_malformed_record_reports_its_starting_physical_line(self):
        self.path.write_text(
            ",".join(HEADER) + "\n"
            "1,complete,2,1,1000,1000,1010,10,0\n"
            '1,complete,2,"2\n",1000,2000,2010,10,0\n',
            encoding="ascii",
        )
        self.assert_invalid(self.path, 3)

    def test_invalid_byte_after_valid_prefix_reports_its_physical_line(self):
        self.path.write_bytes(
            ",".join(HEADER).encode("ascii") + b"\n"
            b"1,complete,2,1,1000,1000,1010,10,0\n"
            b"1,complete,2,2,1000,2000,2010,\xff,0\n"
        )
        self.assert_invalid(self.path, 3)

    def test_missing_path_is_a_validation_error(self):
        self.assert_invalid(self.directory / "missing.csv")


class StatisticsTests(TemporaryCSVTests):
    def test_known_statistics_and_inclusive_linear_percentiles(self):
        statistics = summarize(load_run(FIXTURES / "complete.csv"))
        self.assertEqual(statistics.count, 5)
        self.assertEqual(statistics.minimum_ns, 0)
        self.assertEqual(statistics.maximum_ns, 100)
        self.assertEqual(statistics.mean_ns, 32)
        self.assertEqual(statistics.median_ns, 20)
        self.assertAlmostEqual(statistics.sample_stddev_ns, math.sqrt(1570))
        self.assertAlmostEqual(statistics.p95_ns, 86)
        self.assertAlmostEqual(statistics.p99_ns, 97.2)
        self.assertEqual(statistics.observed_skipped_periods, 3)

    def test_percentiles_sort_latencies_and_retain_duplicate_values(self):
        for latencies, mean, median, p95, p99 in [
            ([100, 0, 30, 10, 20], 32, 20, 86, 97.2),
            ([42, 42, 42], 42, 42, 42, 42),
        ]:
            with self.subTest(latencies=latencies):
                rows = [record(requested=len(latencies), sample=index,
                               deadline=index * 1000,
                               observed=index * 1000 + latency, latency=latency)
                        for index, latency in enumerate(latencies, 1)]
                statistics = summarize(load_run(self.write_rows(rows)))
                self.assertEqual(statistics.mean_ns, mean)
                self.assertEqual(statistics.median_ns, median)
                self.assertAlmostEqual(statistics.p95_ns, p95)
                self.assertAlmostEqual(statistics.p99_ns, p99)

    def test_late_last_observation_does_not_invent_additional_skips(self):
        run = load_run(self.write_rows([
            record(observed=100_000, latency=99_000),
        ]))
        statistics = summarize(run)
        self.assertEqual(statistics.maximum_ns, 99_000)
        self.assertEqual(statistics.observed_skipped_periods, 0)

    def test_empty_statistics_are_explicitly_unavailable(self):
        statistics = summarize(load_run(FIXTURES / "header-only.csv"))
        self.assertEqual(statistics.count, 0)
        self.assertEqual(statistics.observed_skipped_periods, 0)
        for name in ["minimum_ns", "maximum_ns", "mean_ns", "median_ns",
                     "sample_stddev_ns", "p95_ns", "p99_ns"]:
            with self.subTest(statistic=name):
                self.assertIsNone(getattr(statistics, name))

    def test_single_sample_has_no_sample_standard_deviation(self):
        statistics = summarize(load_run(FIXTURES / "single.csv"))
        self.assertEqual(statistics.count, 1)
        self.assertEqual(statistics.minimum_ns, 42)
        self.assertEqual(statistics.maximum_ns, 42)
        self.assertEqual(statistics.mean_ns, 42)
        self.assertEqual(statistics.median_ns, 42)
        self.assertEqual(statistics.p95_ns, 42)
        self.assertEqual(statistics.p99_ns, 42)
        self.assertIsNone(statistics.sample_stddev_ns)

    def test_two_sample_percentiles_interpolate(self):
        rows = [record(requested=2, observed=1000, latency=0),
                record(requested=2, sample=2, deadline=2000, observed=2100,
                       latency=100)]
        statistics = summarize(load_run(self.write_rows(rows)))
        self.assertEqual(statistics.median_ns, 50)
        self.assertAlmostEqual(statistics.sample_stddev_ns, math.sqrt(5000))
        self.assertAlmostEqual(statistics.p95_ns, 95)
        self.assertAlmostEqual(statistics.p99_ns, 99)

    def test_partial_runs_can_be_summarized_without_becoming_complete(self):
        for name in ["interrupted.csv", "error.csv", "truncated.csv"]:
            with self.subTest(name=name):
                run = load_run(FIXTURES / name)
                classification = run.classification
                statistics = summarize(run)
                self.assertEqual(statistics.count, 2)
                self.assertEqual(statistics.mean_ns, 5)
                self.assertEqual(run.classification, classification)
                self.assertNotEqual(run.classification, "complete")

    def test_large_integer_latencies_remain_finite(self):
        rows = [record(interval=1, deadline=1, observed=INT64_MAX,
                       latency=INT64_MAX - 1)]
        statistics = summarize(load_run(self.write_rows(rows)))
        self.assertEqual(statistics.minimum_ns, INT64_MAX - 1)
        self.assertEqual(statistics.maximum_ns, INT64_MAX - 1)
        self.assertTrue(math.isfinite(statistics.mean_ns))
        self.assertTrue(math.isfinite(statistics.p99_ns))


class CLITests(TemporaryCSVTests):
    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "wakeup_latency", *map(str, arguments)],
                              cwd=ROOT, text=True, capture_output=True,
                              timeout=10, check=False)

    def test_validate_complete_json(self):
        path = FIXTURES / "complete.csv"
        result = self.run_cli("validate", path, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = json.loads(result.stdout)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report["source"], str(path))
        self.assertEqual(report["classification"], "complete")
        self.assertEqual(report["declared_status"], "complete")
        self.assertEqual(report["requested_samples"], 5)
        self.assertEqual(report["observed_samples"], 5)
        self.assertEqual(report["interval_ns"], 1000)
        self.assertEqual(report["observed_skipped_periods"], 3)
        self.assertIsNone(report["diagnostic"])
        self.assertIsNone(report["diagnostic_line"])

    def test_summarize_json(self):
        result = self.run_cli("summarize", FIXTURES / "complete.csv", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)[0]
        statistics = report["statistics"]
        self.assertEqual(statistics["count"], 5)
        self.assertEqual(statistics["minimum_ns"], 0)
        self.assertEqual(statistics["maximum_ns"], 100)
        self.assertEqual(statistics["mean_ns"], 32)
        self.assertEqual(statistics["median_ns"], 20)
        self.assertAlmostEqual(statistics["sample_stddev_ns"], math.sqrt(1570))
        self.assertAlmostEqual(statistics["p95_ns"], 86)
        self.assertAlmostEqual(statistics["p99_ns"], 97.2)
        self.assertEqual(statistics["observed_skipped_periods"], 3)

    def test_valid_partial_classifications_return_one(self):
        for name, classification in [
            ("interrupted.csv", "interrupted"), ("error.csv", "error"),
            ("truncated.csv", "truncated"), ("header-only.csv", "header-only"),
        ]:
            with self.subTest(name=name):
                result = self.run_cli("summarize", FIXTURES / name, "--json")
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)[0]
                self.assertEqual(report["classification"], classification)
                self.assertIn("statistics", report)

    def test_malformed_input_has_filename_row_and_no_statistics(self):
        path = FIXTURES / "inconsistent-latency.csv"
        result = self.run_cli("summarize", path, "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(path), result.stderr)
        self.assertIn(":2", result.stderr)
        report = json.loads(result.stdout)[0]
        self.assertEqual(report["classification"], "invalid")
        self.assertTrue(report["diagnostic"])
        self.assertNotIn("statistics", report)

    def test_multiple_files_continue_and_invalid_exit_has_precedence(self):
        paths = [FIXTURES / "complete.csv", FIXTURES / "torn-row.csv",
                 FIXTURES / "interrupted.csv", FIXTURES / "single.csv"]
        result = self.run_cli("validate", *paths, "--json")
        self.assertEqual(result.returncode, 2)
        reports = json.loads(result.stdout)
        self.assertEqual([report["source"] for report in reports],
                         [str(path) for path in paths])
        self.assertEqual([report["classification"] for report in reports],
                         ["complete", "invalid", "interrupted", "complete"])

    def test_multiple_complete_files_return_zero_and_valid_partial_returns_one(self):
        for name, expected in [("single.csv", 0), ("interrupted.csv", 1)]:
            with self.subTest(name=name):
                result = self.run_cli("validate", FIXTURES / "complete.csv",
                                      FIXTURES / name, "--json")
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(len(json.loads(result.stdout)), 2)

    def test_arbitrary_filename_with_spaces(self):
        source = self.write_rows([record()])
        arbitrary = self.directory / "synthetic run with spaces.csv"
        source.rename(arbitrary)
        result = self.run_cli("summarize", arbitrary, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)[0]
        self.assertEqual(report["source"], str(arbitrary))
        self.assertEqual(report["statistics"]["count"], 1)

    def test_missing_file_returns_two_and_other_files_are_processed(self):
        missing = self.directory / "missing.csv"
        result = self.run_cli("validate", missing, FIXTURES / "complete.csv", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(missing), result.stderr)
        reports = json.loads(result.stdout)
        self.assertEqual([report["classification"] for report in reports],
                         ["invalid", "complete"])

    def test_text_output_exposes_classification_and_statistics(self):
        path = FIXTURES / "truncated.csv"
        result = self.run_cli("summarize", path)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(str(path), result.stdout)
        self.assertIn("truncated", result.stdout)
        self.assertIn("p95", result.stdout.lower())
        self.assertIn("p99", result.stdout.lower())

    def test_help_and_invalid_invocation(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validate", result.stdout)
        self.assertIn("summarize", result.stdout)
        for arguments in [[], ["validate"], ["unknown", "file.csv"],
                          ["summarize", "--unknown"]]:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr)

    @unittest.skipUnless(Path("/dev/full").exists(), "/dev/full unavailable")
    def test_output_failure_returns_two_without_shutdown_flush_error(self):
        with open("/dev/full", "wb") as full:
            result = subprocess.run(
                [sys.executable, "-m", "wakeup_latency", "summarize",
                 str(FIXTURES / "complete.csv")],
                cwd=ROOT, stdout=full, stderr=subprocess.PIPE, text=True,
                timeout=10, check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("cannot write analysis output", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
