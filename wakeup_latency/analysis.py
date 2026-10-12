"""One standard-library pipeline for schema validation and run statistics."""

import csv
from dataclasses import dataclass
from fractions import Fraction
import os
import statistics
from typing import TextIO


FIELDS = (
    "schema_version", "run_status", "requested_samples", "sample", "interval_ns",
    "deadline_ns", "observed_ns", "latency_ns", "skipped_periods",
)
INT64_MAX = (1 << 63) - 1
UINT64_MAX = (1 << 64) - 1
STATUSES = frozenset(("complete", "interrupted", "error"))


class ValidationError(ValueError):
    """An invalid input, with a physical CSV line (zero for file I/O errors)."""

    def __init__(self, source: str, line: int, message: str):
        self.source = source
        self.line = line
        self.message = message
        super().__init__(f"{source}:{line}: {message}")


@dataclass(frozen=True)
class Sample:
    sample: int
    deadline_ns: int
    observed_ns: int
    latency_ns: int
    skipped_periods: int


@dataclass(frozen=True)
class Run:
    source: str
    samples: tuple[Sample, ...]
    declared_status: str | None
    requested_samples: int | None
    interval_ns: int | None
    classification: str
    diagnostic: str | None = None
    diagnostic_line: int | None = None


@dataclass(frozen=True)
class Statistics:
    count: int
    minimum_ns: int | None
    maximum_ns: int | None
    mean_ns: int | float | None
    median_ns: int | float | None
    sample_stddev_ns: float | None
    p95_ns: int | float | None
    p99_ns: int | float | None
    observed_skipped_periods: int


def _integer(text: str, field: str, source: str, line: int) -> int:
    # Check before int() so huge fields cannot leak its digit-limit exception.
    if not text.isascii() or not text.isdecimal():
        raise ValidationError(source, line,
                              f"{field} must be a nonnegative ASCII decimal integer")
    significant = text.lstrip("0") or "0"
    limit = (UINT64_MAX if field in
             ("schema_version", "requested_samples", "sample", "skipped_periods")
             else INT64_MAX)
    if len(significant) > len(str(limit)):
        raise ValidationError(source, line, f"{field} exceeds its integer range")
    value = int(significant)
    if value > limit:
        raise ValidationError(source, line, f"{field} exceeds its integer range")
    if field in ("requested_samples", "sample", "interval_ns") and value == 0:
        raise ValidationError(source, line, f"{field} must be positive")
    return value


def _read_run(stream: TextIO, source: str) -> Run:
    reader = csv.reader(stream, strict=True)
    samples = []
    declared_status = None
    requested_samples = None
    interval_ns = None
    next_line = 1
    try:
        header = next(reader, None)
        if header is None:
            raise ValidationError(source, 1, "empty file: missing schema v1 header")
        if tuple(header) != FIELDS:
            raise ValidationError(source, 1, "expected the exact schema v1 header")
        while True:
            next_line = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if tuple(row) == FIELDS:
                raise ValidationError(source, next_line, "duplicate header")
            if len(row) != len(FIELDS):
                raise ValidationError(
                    source, next_line,
                    f"expected {len(FIELDS)} fields, found {len(row)} "
                    "(malformed or possibly truncated record)",
                )
            for field, text in zip(FIELDS, row):
                if text == "":
                    raise ValidationError(source, next_line, f"missing value for {field}")
            values = {
                field: _integer(text, field, source, next_line)
                for field, text in zip(FIELDS, row) if field != "run_status"
            }
            if values["schema_version"] != 1:
                raise ValidationError(source, next_line, "schema_version must be 1")
            status = row[1]
            if status not in STATUSES:
                raise ValidationError(source, next_line,
                                      "run_status must be complete, interrupted, or error")
            if not samples:
                declared_status = status
                requested_samples = values["requested_samples"]
                interval_ns = values["interval_ns"]
            elif (status != declared_status or
                  values["requested_samples"] != requested_samples or
                  values["interval_ns"] != interval_ns):
                raise ValidationError(
                    source, next_line,
                    "run_status, requested_samples, and interval_ns must remain consistent",
                )
            if values["sample"] != len(samples) + 1:
                raise ValidationError(source, next_line,
                                      f"expected sample {len(samples) + 1} in order")
            if values["sample"] > requested_samples:
                raise ValidationError(source, next_line,
                                      "observations exceed requested_samples")
            deadline = values["deadline_ns"]
            observed = values["observed_ns"]
            latency = values["latency_ns"]
            previous_deadline = samples[-1].deadline_ns if samples else 0
            if deadline - previous_deadline != (values["skipped_periods"] + 1) * interval_ns:
                raise ValidationError(source, next_line,
                                      "deadline progression does not match skipped_periods")
            if observed < deadline:
                raise ValidationError(source, next_line,
                                      "observed_ns must be at or after deadline_ns")
            if latency != observed - deadline:
                raise ValidationError(source, next_line,
                                      "latency_ns must equal observed_ns - deadline_ns")
            if samples:
                if observed < samples[-1].observed_ns:
                    raise ValidationError(source, next_line,
                                          "observed_ns must not move backwards")
                if deadline <= samples[-1].observed_ns:
                    raise ValidationError(
                        source, next_line,
                        "deadline_ns must be strictly after the previous observed_ns "
                        "(expired periods must be skipped)",
                    )
            samples.append(Sample(values["sample"], deadline, observed, latency,
                                  values["skipped_periods"]))
    except csv.Error as error:
        raise ValidationError(source, next_line, f"malformed CSV: {error}") from error

    if not samples:
        return Run(source, (), None, None, None, "header-only",
                   "header-only CSV: version values and run metadata are unknown", next_line)
    if declared_status == "complete" and len(samples) < requested_samples:
        return Run(
            source, tuple(samples), declared_status, requested_samples, interval_ns,
            "truncated",
            f"declares complete but contains {len(samples)} of {requested_samples} "
            "requested observations: incomplete data, possibly truncated",
            next_line,
        )
    return Run(source, tuple(samples), declared_status, requested_samples, interval_ns,
               declared_status)


def load_run(path: str | os.PathLike[str]) -> Run:
    """Validate every row before returning a classified run; never salvage bad rows.

    A valid complete prefix with missing rows is classified as truncated. The
    same loss cannot be detected for interrupted/error runs without external
    process status. Header-only runs have no recoverable version or metadata.
    """
    source = os.fspath(path)
    try:
        # Surrogate escapes keep invalid bytes in their physical CSV record,
        # where header/field validation reports them with the correct line.
        with open(path, encoding="ascii", errors="surrogateescape", newline="") as stream:
            return _read_run(stream, source)
    except OSError as error:
        raise ValidationError(source, 0, f"cannot read file: {error.strerror or error}") from error


def _percentile(ordered: list[int], percent: int) -> int | float:
    """Inclusive linear interpolation (type 7); exact index arithmetic."""
    index, remainder = divmod((len(ordered) - 1) * percent, 100)
    if remainder == 0:
        return ordered[index]
    value = Fraction(ordered[index] * (100 - remainder) +
                     ordered[index + 1] * remainder, 100)
    return value.numerator if value.denominator == 1 else float(value)


def summarize(run: Run) -> Statistics:
    """Statistics of validated observed rows, without changing run classification."""
    latencies = sorted(sample.latency_ns for sample in run.samples)
    count = len(latencies)
    skipped = sum(sample.skipped_periods for sample in run.samples)
    if not count:
        return Statistics(0, None, None, None, None, None, None, None, skipped)
    return Statistics(
        count, latencies[0], latencies[-1], statistics.mean(latencies),
        statistics.median(latencies), statistics.stdev(latencies) if count > 1 else None,
        _percentile(latencies, 95), _percentile(latencies, 99), skipped,
    )
