"""CLI for validation, summaries, and optional headless plots."""

import argparse
from dataclasses import asdict
import json
import os
import sys

from .analysis import INT64_MAX, ValidationError, load_run, summarize


def _report(path: str, include_statistics: bool) -> tuple[dict, int]:
    try:
        run = load_run(path)
    except ValidationError as error:
        print(error, file=sys.stderr)
        return {"source": error.source, "classification": "invalid",
                "declared_status": None, "requested_samples": None,
                "observed_samples": None, "interval_ns": None,
                "observed_skipped_periods": None, "diagnostic": error.message,
                "diagnostic_line": error.line}, 2
    result = {
        "source": run.source, "classification": run.classification,
        "declared_status": run.declared_status,
        "requested_samples": run.requested_samples,
        "observed_samples": len(run.samples), "interval_ns": run.interval_ns,
        "observed_skipped_periods": sum(row.skipped_periods for row in run.samples),
        "diagnostic": run.diagnostic, "diagnostic_line": run.diagnostic_line,
    }
    if include_statistics:
        result["statistics"] = asdict(summarize(run))
    if run.diagnostic:
        print(f"{run.source}:{run.diagnostic_line}: {run.diagnostic}", file=sys.stderr)
    return result, 0 if run.classification == "complete" else 1


def _display(report: dict) -> None:
    print(f"{report['source']}: {report['classification']}")
    if report["classification"] == "invalid":
        return
    for field in ("declared_status", "requested_samples", "observed_samples",
                  "interval_ns", "observed_skipped_periods"):
        value = report[field]
        print(f"  {field}: {value if value is not None else 'unknown'}")
    if "statistics" in report:
        for field, value in report["statistics"].items():
            print(f"  {field}: {value if value is not None else 'unavailable'}")


def _nanoseconds(text: str) -> int:
    if not text.isascii() or not text.isdecimal():
        raise argparse.ArgumentTypeError("expected nonnegative ASCII integer nanoseconds")
    significant = text.lstrip("0") or "0"
    if len(significant) > 19 or int(significant) > INT64_MAX:
        raise argparse.ArgumentTypeError("nanoseconds exceed signed 64-bit range")
    return int(significant)


def _plot_report(arguments) -> tuple[dict, int]:
    from .plotting import PlotError, plot_run

    try:
        result = plot_run(arguments.file, arguments.output_dir, bins=arguments.bins,
                          latency_range_ns=arguments.latency_range_ns,
                          overwrite=arguments.overwrite)
    except (ValidationError, PlotError) as error:
        print(f"{arguments.file}: {error}", file=sys.stderr)
        return {"source": arguments.file, "classification": "invalid",
                "diagnostic": str(error)}, 2
    return asdict(result), 0 if result.classification == "complete" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate, summarize, or plot application-observed wake-up lateness CSV v1.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "summarize"):
        command = commands.add_parser(name)
        command.add_argument("files", nargs="+", help="CSV files to inspect independently")
        command.add_argument("--json", action="store_true", help="emit a JSON report list")
    plot = commands.add_parser("plot", help="save histogram, ECDF, and sample PNGs (optional Matplotlib)")
    plot.add_argument("file", help="CSV file to validate and plot")
    plot.add_argument("--output-dir", required=True, help="directory for the three PNG files")
    plot.add_argument("--bins", type=_nanoseconds, default=50, help="histogram bins (1..10000, default 50)")
    plot.add_argument("--latency-range-ns", nargs=2, type=_nanoseconds, metavar=("MIN", "MAX"),
                      help="inclusive distribution zoom; sample plot keeps all observations")
    plot.add_argument("--overwrite", action="store_true", help="replace existing regular PNG files")
    plot.add_argument("--json", action="store_true", help="emit a JSON report list")
    arguments = parser.parse_args(argv)
    reports = []
    exit_status = 0
    if arguments.command == "plot":
        report, exit_status = _plot_report(arguments)
        reports.append(report)
    else:
        for path in arguments.files:
            report, status = _report(path, arguments.command == "summarize")
            reports.append(report)
            exit_status = max(exit_status, status)
    try:
        if arguments.json:
            print(json.dumps(reports, indent=2, allow_nan=False))
        else:
            for report in reports:
                if arguments.command == "plot":
                    print(f"{report['source']}: {report['classification']}")
                    if "paths" in report:
                        print(f"  distribution shown: {report['shown']}/{report['observations']}; "
                              f"below range: {report['below_range']}; above range: {report['above_range']}")
                        for path in report["paths"]:
                            print(f"  plot: {path}")
                else:
                    _display(report)
        sys.stdout.flush()
    except (OSError, ValueError) as error:
        print(f"cannot write analysis output: {error}", file=sys.stderr)
        # Python otherwise retries a failed buffered flush at shutdown and
        # replaces our defined status with 120. Discard that pending output.
        try:
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except OSError:
            pass
        return 2
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
