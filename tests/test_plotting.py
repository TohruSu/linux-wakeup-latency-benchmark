"""Deterministic plotting tests; real rendering is optional with Matplotlib."""

import builtins
from contextlib import ExitStack
import csv
from dataclasses import FrozenInstanceError
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from wakeup_latency import ValidationError
from wakeup_latency.plotting import PlotError, _ecdf, plot_run


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "schema-v1"
PLOT_NAMES = ("histogram.png", "ecdf.png", "latency-vs-sample.png")
HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None
HEADER = [
    "schema_version", "run_status", "requested_samples", "sample", "interval_ns",
    "deadline_ns", "observed_ns", "latency_ns", "skipped_periods",
]
INT64_MAX = (1 << 63) - 1


class TemporaryPlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wakeup-plot-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "plots"

    def write_latencies(self, latencies, *, status="complete", requested=None):
        """Keep each next deadline strictly after the preceding observation."""
        path = self.directory / "synthetic.csv"
        if requested is None:
            requested = len(latencies)
        with path.open("w", encoding="ascii", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(HEADER)
            previous_deadline = 0
            previous_observed = 0
            interval = 1000
            for index, latency in enumerate(latencies, 1):
                deadline = max(previous_deadline + interval,
                               (previous_observed // interval + 1) * interval)
                skipped = (deadline - previous_deadline) // interval - 1
                observed = deadline + latency
                writer.writerow([1, status, requested, index, interval, deadline,
                                 observed, latency, skipped])
                previous_deadline = deadline
                previous_observed = observed
        return path

    def run_cli(self, *arguments, environment=None):
        env = os.environ.copy()
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        if environment:
            env.update(environment)
        return subprocess.run(
            [sys.executable, "-m", "wakeup_latency", *map(str, arguments)],
            cwd=ROOT, env=env, text=True, capture_output=True,
            timeout=30, check=False,
        )

    def assert_pngs(self, report):
        self.assertEqual(tuple(Path(path).name for path in report.paths), PLOT_NAMES)
        for path in report.paths:
            with self.subTest(path=path):
                data = Path(path).read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(data[12:16], b"IHDR")
                width, height = struct.unpack(">II", data[16:24])
                self.assertGreater(width, 0)
                self.assertGreater(height, 0)


class StandardLibraryPlotTests(TemporaryPlotTests):
    def test_module_import_does_not_load_matplotlib(self):
        script = (
            "import sys; import wakeup_latency; import wakeup_latency.plotting; "
            "assert not any(name == 'matplotlib' or name.startswith('matplotlib.') "
            "for name in sys.modules), 'Matplotlib imported eagerly'"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                text=True, capture_output=True, timeout=10,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_validate_and_summarize_work_with_matplotlib_blocked(self):
        script = """
import builtins
import sys
original_import = builtins.__import__
def unavailable(name, *args, **kwargs):
    if name == 'matplotlib' or name.startswith('matplotlib.'):
        raise ImportError('synthetic missing optional dependency')
    return original_import(name, *args, **kwargs)
builtins.__import__ = unavailable
from wakeup_latency.__main__ import main
raise SystemExit(main(sys.argv[1:]))
"""
        for command in ("validate", "summarize"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-c", script, command,
                     str(FIXTURES / "complete.csv"), "--json"],
                    cwd=ROOT, text=True, capture_output=True,
                    timeout=10, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('"classification": "complete"', result.stdout)

    def test_missing_matplotlib_has_actionable_error_and_no_artifacts(self):
        original_import = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "matplotlib" or name.startswith("matplotlib."):
                raise ImportError("synthetic missing optional dependency")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=unavailable):
            with self.assertRaises(PlotError) as caught:
                plot_run(FIXTURES / "complete.csv", self.output)
        self.assertIn("matplotlib", str(caught.exception).lower())
        self.assertTrue("install" in str(caught.exception).lower() or
                        "optional" in str(caught.exception).lower())
        self.assertFalse(self.output.exists())

    def test_cli_missing_matplotlib_returns_two_without_traceback(self):
        script = """
import builtins
import sys
original_import = builtins.__import__
def unavailable(name, *args, **kwargs):
    if name == 'matplotlib' or name.startswith('matplotlib.'):
        raise ImportError('synthetic missing optional dependency')
    return original_import(name, *args, **kwargs)
builtins.__import__ = unavailable
from wakeup_latency.__main__ import main
raise SystemExit(main(sys.argv[1:]))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, "plot",
             str(FIXTURES / "complete.csv"), "--output-dir", str(self.output)],
            cwd=ROOT, text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("matplotlib", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())

    def test_ecdf_sorts_and_counts_duplicates(self):
        x, y = _ecdf([30, 10, 10, 0, 30])
        self.assertEqual(x, [0, 10, 30])
        self.assertEqual(y, [0.2, 0.6, 1.0])

    def test_ecdf_single_value_reaches_one(self):
        self.assertEqual(_ecdf([42]), ([42], [1.0]))
        self.assertEqual(_ecdf([42, 42, 42]), ([42], [1.0]))

    def test_invalid_csv_reuses_filename_and_line_diagnostics(self):
        for name, line in [("negative-latency.csv", 2),
                           ("inconsistent-latency.csv", 2),
                           ("invalid-skips.csv", 3),
                           ("duplicate-header.csv", 3),
                           ("missing-value.csv", 2),
                           ("torn-row.csv", 3)]:
            with self.subTest(name=name):
                path = FIXTURES / name
                with self.assertRaises(ValidationError) as caught:
                    plot_run(path, self.output)
                self.assertEqual(caught.exception.source, str(path))
                self.assertEqual(caught.exception.line, line)
                self.assertFalse(self.output.exists())

    def test_nonfinite_and_missing_values_are_rejected(self):
        path = self.write_latencies([10])
        contents = path.read_text(encoding="ascii")
        for value in ("NaN", "nan", "inf", "-inf", ""):
            with self.subTest(value=value):
                path.write_text(contents.replace(",1010,10,0", f",1010,{value},0"),
                                encoding="ascii")
                with self.assertRaises(ValidationError):
                    plot_run(path, self.output)
                self.assertFalse(self.output.exists())

    def test_header_only_cannot_produce_misleading_plots(self):
        with self.assertRaises(PlotError) as caught:
            plot_run(FIXTURES / "header-only.csv", self.output)
        self.assertIn("header-only", str(caught.exception).lower())
        self.assertFalse(self.output.exists())

    def test_invalid_settings_create_no_artifacts(self):
        cases = [
            {"bins": 0}, {"bins": -1}, {"bins": 1.5}, {"bins": True},
            {"latency_range_ns": (-1, 10)},
            {"latency_range_ns": (10, 10)},
            {"latency_range_ns": (20, 10)},
            {"latency_range_ns": (0, INT64_MAX + 1)},
            {"latency_range_ns": (0.0, 10)},
            {"latency_range_ns": (False, 10)},
            {"latency_range_ns": (0,)},
            {"latency_range_ns": (0, 1, 2)},
        ]
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaises(PlotError):
                    plot_run(FIXTURES / "complete.csv", self.output, **options)
                self.assertFalse(self.output.exists())

    def test_cli_invalid_settings_and_data_return_two(self):
        common = ["plot", str(FIXTURES / "complete.csv"),
                  "--output-dir", str(self.output)]
        cases = [
            common + ["--bins", "0"], common + ["--bins", "NaN"],
            common + ["--latency-range-ns", "-1", "10"],
            common + ["--latency-range-ns", "10", "10"],
            common + ["--latency-range-ns", "nan", "20"],
            common + ["--latency-range-ns", "0", str(INT64_MAX + 1)],
            ["plot", str(FIXTURES / "negative-latency.csv"),
             "--output-dir", str(self.output)],
            ["plot", str(FIXTURES / "header-only.csv"),
             "--output-dir", str(self.output)],
            ["plot", str(self.directory / "missing.csv"),
             "--output-dir", str(self.output)],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.output.exists())


@unittest.skipUnless(HAS_MATPLOTLIB, "optional Matplotlib is not installed")
class RenderingTests(TemporaryPlotTests):
    def setUp(self):
        super().setUp()
        from matplotlib.figure import Figure
        self.figure_type = Figure

    def capture_figures(self, path, **options):
        figures = []
        original_savefig = self.figure_type.savefig
        with ExitStack() as patches:
            def savefig(figure, *arguments, **keywords):
                figures.append(figure)
                result = original_savefig(figure, *arguments, **keywords)
                # Preserve rendered axes for inspection while the collector
                # performs its normal finally-block resource cleanup. Restore
                # clear immediately after plotting, and actually clear it at
                # test cleanup; production rendering remains unchanged.
                self.addCleanup(figure.clear)
                patches.enter_context(mock.patch.object(figure, "clear"))
                return result

            with mock.patch.object(self.figure_type, "savefig", new=savefig):
                report = plot_run(path, self.output, **options)
        self.assertEqual(len(figures), 3)
        return report, figures

    @staticmethod
    def figure_text(figure):
        texts = [text.get_text() for text in figure.texts]
        for axes in figure.axes:
            texts.extend([axes.get_title(), axes.get_xlabel(), axes.get_ylabel()])
            texts.extend(text.get_text() for text in axes.texts)
        return "\n".join(texts)

    @staticmethod
    def histogram_total(axes):
        # The production histogram uses stairs with exact integer-bin counts.
        return sum(sum(patch.get_data().values) for patch in axes.patches)

    def test_complete_run_writes_three_pngs_and_immutable_report(self):
        source = FIXTURES / "complete.csv"
        report = plot_run(source, self.output)
        self.assertEqual(report.source, str(source))
        self.assertEqual(report.classification, "complete")
        self.assertEqual(report.observations, 5)
        self.assertEqual(report.shown, 5)
        self.assertEqual(report.below_range, 0)
        self.assertEqual(report.above_range, 0)
        self.assertIsNone(report.range_ns)
        self.assertIsInstance(report.paths, tuple)
        with self.assertRaises(FrozenInstanceError):
            report.classification = "error"
        self.assert_pngs(report)

    def test_partial_classifications_and_counts_are_visible_on_every_plot(self):
        for name, classification in [("interrupted.csv", "interrupted"),
                                     ("error.csv", "error"),
                                     ("truncated.csv", "truncated")]:
            with self.subTest(name=name):
                self.output = self.directory / classification
                report, figures = self.capture_figures(FIXTURES / name)
                self.assertEqual(report.classification, classification)
                self.assertEqual(report.observations, 2)
                for figure in figures:
                    text = self.figure_text(figure)
                    self.assertIn(classification, text.lower())
                    self.assertIn(name, text)
                    self.assertIn("2", text)
                    self.assertIn("5", text)
                self.assert_pngs(report)

    def test_axis_labels_have_correct_units_and_descriptive_annotation(self):
        _, figures = self.capture_figures(FIXTURES / "complete.csv")
        histogram, ecdf, samples = [figure.axes[0] for figure in figures]
        for axes in (histogram, ecdf):
            self.assertIn("lateness", axes.get_xlabel().lower())
            self.assertIn("ns", axes.get_xlabel())
        self.assertIn("count", histogram.get_ylabel().lower())
        self.assertIn("probability", ecdf.get_ylabel().lower())
        self.assertIn("all observations", ecdf.get_ylabel().lower())
        self.assertIn("sample", samples.get_xlabel().lower())
        self.assertIn("lateness", samples.get_ylabel().lower())
        self.assertIn("ns", samples.get_ylabel())
        for figure in figures:
            text = self.figure_text(figure).lower()
            self.assertIn("descriptive", text)
            self.assertIn("guarantee", text)

    def test_zoom_counts_are_inclusive_and_ecdf_uses_full_denominator(self):
        report, figures = self.capture_figures(
            FIXTURES / "complete.csv", latency_range_ns=(10, 30), bins=3)
        self.assertEqual(report.range_ns, (10, 30))
        self.assertEqual(report.shown, 3)
        self.assertEqual(report.below_range, 1)
        self.assertEqual(report.above_range, 1)
        self.assertEqual(report.shown + report.below_range + report.above_range,
                         report.observations)
        histogram, ecdf, samples = [figure.axes[0] for figure in figures]
        self.assertEqual(self.histogram_total(histogram), 3)
        self.assertEqual(tuple(ecdf.get_xlim()), (10, 30))
        points = dict(zip(ecdf.lines[0].get_xdata(), ecdf.lines[0].get_ydata()))
        self.assertAlmostEqual(points[10], 0.4)
        self.assertAlmostEqual(points[30], 0.8)
        # Inclusive boundary values remain visible even behind an axes spine.
        self.assertEqual(ecdf.collections[0].get_offsets().tolist(),
                         [[10.0, 0.4], [30.0, 0.8]])
        self.assertEqual(list(samples.lines[0].get_xdata()), [1, 2, 3, 4, 5])
        self.assertEqual(list(samples.lines[0].get_ydata()), [0, 10, 20, 30, 100])
        for figure in figures[:2]:
            text = self.figure_text(figure).lower()
            self.assertIn("below", text)
            self.assertIn("above", text)
        self.assert_pngs(report)

    def test_zoom_with_no_visible_observations_is_explicit(self):
        report, figures = self.capture_figures(
            FIXTURES / "complete.csv", latency_range_ns=(40, 50))
        self.assertEqual((report.shown, report.below_range, report.above_range),
                         (0, 4, 1))
        self.assertEqual(self.histogram_total(figures[0].axes[0]), 0)
        self.assert_pngs(report)

    def test_single_observation_and_constant_distribution_render(self):
        for latencies in ([42], [0, 0, 0], [42, 42, 42]):
            with self.subTest(latencies=latencies):
                self.output = self.directory / ("plots-" + str(latencies))
                source = self.write_latencies(latencies)
                report = plot_run(source, self.output)
                self.assertEqual(report.observations, len(latencies))
                self.assert_pngs(report)

    def test_full_integer_range_is_plottable(self):
        source = self.write_latencies([INT64_MAX - 1000])
        report = plot_run(source, self.output, latency_range_ns=(0, INT64_MAX))
        self.assertEqual(report.observations, 1)
        self.assertEqual(report.shown, 1)
        self.assert_pngs(report)

    def test_unrepresentable_display_range_is_rejected_explicitly(self):
        source = self.write_latencies([INT64_MAX - 1000])
        with self.assertRaises(PlotError):
            plot_run(source, self.output,
                     latency_range_ns=(INT64_MAX - 1001, INT64_MAX - 1000))
        self.assertFalse(self.output.exists())

    def test_existing_destination_is_preserved_without_partial_generation(self):
        self.output.mkdir()
        existing = self.output / "ecdf.png"
        existing.write_bytes(b"preserve existing content")
        with self.assertRaises(PlotError):
            plot_run(FIXTURES / "complete.csv", self.output)
        self.assertEqual(existing.read_bytes(), b"preserve existing content")
        self.assertEqual(list(self.output.iterdir()), [existing])

    def test_explicit_overwrite_replaces_regular_files(self):
        self.output.mkdir()
        for name in PLOT_NAMES:
            (self.output / name).write_bytes(b"old content")
        report = plot_run(FIXTURES / "complete.csv", self.output, overwrite=True)
        self.assert_pngs(report)

    def test_final_output_directory_symlink_is_rejected(self):
        target = self.directory / "target"
        target.mkdir()
        self.output.symlink_to(target, target_is_directory=True)
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                with self.assertRaises(PlotError):
                    plot_run(FIXTURES / "complete.csv", self.output,
                             overwrite=overwrite)
                self.assertEqual(list(target.iterdir()), [])

    def test_parent_directory_symlink_is_permitted(self):
        target = self.directory / "target"
        target.mkdir()
        link = self.directory / "parent-link"
        link.symlink_to(target, target_is_directory=True)
        report = plot_run(FIXTURES / "complete.csv", link / "plots")
        self.assertTrue((target / "plots").is_dir())
        self.assert_pngs(report)

    def test_final_output_file_symlink_is_preserved_even_with_overwrite(self):
        self.output.mkdir()
        target = self.directory / "valuable-file"
        target.write_bytes(b"preserve symlink target")
        link = self.output / "ecdf.png"
        link.symlink_to(target)
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                with self.assertRaises(PlotError):
                    plot_run(FIXTURES / "complete.csv", self.output,
                             overwrite=overwrite)
                self.assertTrue(link.is_symlink())
                self.assertEqual(target.read_bytes(), b"preserve symlink target")
                self.assertEqual(list(self.output.iterdir()), [link])

    def test_output_directory_cannot_be_a_regular_file(self):
        self.output.write_bytes(b"preserve file")
        with self.assertRaises(PlotError):
            plot_run(FIXTURES / "complete.csv", self.output)
        self.assertEqual(self.output.read_bytes(), b"preserve file")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation unavailable")
    def test_fifo_destination_is_rejected_without_opening_it(self):
        self.output.mkdir()
        fifo = self.output / "ecdf.png"
        os.mkfifo(fifo)
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                result = self.run_cli("plot", FIXTURES / "complete.csv",
                                      "--output-dir", self.output,
                                      *(["--overwrite"] if overwrite else []))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(list(self.output.iterdir()), [fifo])

    def test_save_failure_is_reported_without_false_success(self):
        with mock.patch.object(self.figure_type, "savefig",
                               side_effect=OSError("synthetic write failure")):
            with self.assertRaises(PlotError) as caught:
                plot_run(FIXTURES / "complete.csv", self.output)
        self.assertIn("synthetic write failure", str(caught.exception))

    def test_cli_is_headless_even_with_interactive_backend_environment(self):
        result = self.run_cli("plot", FIXTURES / "complete.csv", "--output-dir",
                              self.output, environment={"MPLBACKEND": "TkAgg"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("complete", result.stdout)
        for name in PLOT_NAMES:
            self.assertTrue((self.output / name).is_file())

    def test_cli_partial_statuses_return_one_and_remain_visible(self):
        for name, classification in [("interrupted.csv", "interrupted"),
                                     ("error.csv", "error"),
                                     ("truncated.csv", "truncated")]:
            with self.subTest(name=name):
                destination = self.directory / classification
                result = self.run_cli("plot", FIXTURES / name,
                                      "--output-dir", destination)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(classification, result.stdout)
                self.assertTrue(all((destination / name).is_file()
                                    for name in PLOT_NAMES))

    def test_cli_zoom_reports_outside_observations(self):
        result = self.run_cli("plot", FIXTURES / "complete.csv", "--output-dir",
                              self.output, "--bins", "3",
                              "--latency-range-ns", "10", "30")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("below", result.stdout.lower())
        self.assertIn("above", result.stdout.lower())
        self.assertIn("3", result.stdout)

    @unittest.skipUnless(Path("/dev/full").exists(), "/dev/full unavailable")
    def test_cli_stdout_failure_returns_two(self):
        with open("/dev/full", "wb") as full:
            result = subprocess.run(
                [sys.executable, "-m", "wakeup_latency", "plot",
                 str(FIXTURES / "complete.csv"), "--output-dir", str(self.output)],
                cwd=ROOT, stdout=full, stderr=subprocess.PIPE,
                text=True, timeout=30, check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
