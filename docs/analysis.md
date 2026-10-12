# CSV validation and descriptive statistics

The `wakeup_latency` package provides one validation pipeline for CSV schema v1. Validation and statistics require Python 3.10 or newer and use only the standard library; plotting optionally requires Matplotlib. Run these examples from the repository root, or replace the fixture paths with your CSV files:

```sh
python3 -m wakeup_latency validate tests/fixtures/schema-v1/complete.csv
python3 -m wakeup_latency summarize tests/fixtures/schema-v1/complete.csv
python3 -m wakeup_latency summarize tests/fixtures/schema-v1/complete.csv \
    tests/fixtures/schema-v1/complete-no-final-newline.csv --json
python3 -m wakeup_latency summarize tests/fixtures/schema-v1/complete.csv --json
make test-analysis
```

Each file is an independent run; samples from separate files are never pooled. `--json` writes a list of per-file reports to stdout, with diagnostics on stderr. Malformed files have classification `invalid` and no statistics. The commands inspect files without modifying them.

## Validation

The validator requires the exact nine parsed header fields, in order, and schema version `1` in every observation. Normal CSV quoting, LF or CRLF, and a final complete record without a newline are accepted. Empty files, duplicate headers, blank rows, malformed quoting, extra or missing fields, and empty values are rejected. Header-only files are classified separately: they have no version **value** or run metadata to verify.

Numeric fields must contain nonnegative ASCII decimal digits; signs, whitespace, decimal points, and Unicode digits are rejected. Leading zeros are accepted. Interval, relative timestamps, and latency must fit signed 64-bit nanoseconds; count, sample index, and skipped periods must fit unsigned 64-bit integers. Interval, requested count, and sample index must be positive. These rules do not impose the current collector's memory cap or interval granularity as additional schema requirements.

Each row must keep its requested count, interval, and supported run status consistent. Samples start at 1 with no gaps and never exceed the requested count. The validator checks the latency equation, nondecreasing observation timestamps, and deadline progression including first-row skipped periods. Every deadline after the first must be strictly greater than the preceding observation: the collector selects it after a fresh monotonic reading, skipping expired slots.

Failures include the filename and physical record-start line. File access failures use line 0; EOF count mismatches use the next physical line. A malformed last row is rejected entirely; the package does not silently salvage its preceding rows for statistics.

## Run classification and completion limits

| Classification | Meaning |
| --- | --- |
| `complete` | All rows are valid, declare `complete`, and match the requested count. |
| `interrupted` | Valid rows declare `interrupted`; cancellation remains explicit even if all requested observations are present. |
| `error` | Valid rows declare a collector measurement error; their count does not turn the run into a complete run. |
| `truncated` | Valid rows declare `complete` but fewer than the requested count are present. This verifies incompleteness; physical truncation is one possible cause. |
| `header-only` | The exact header is present without observations. Requested count, interval, status, and version values are unknown. |

The CLI returns 0 for all-complete CSV inputs, 1 if any structurally valid input has another classification, and 2 if any file is malformed/unreadable, arguments are invalid, or writing the analysis report fails. Every file is inspected even when another fails. Summary reports retain the classification and declared status while calculating descriptive statistics of the valid observed rows, including an explicitly classified truncated prefix.

Schema v1 has no completed-count footer. A valid interrupted/error prefix may itself have lost rows during output; CSV alone cannot detect this. Even a full `complete` CSV cannot prove the collector exited successfully, because a write/flush/close failure has a separate process status. Preserve collector exit codes and stderr when interpreting measurements. The package does not assume process success or recover missing metadata.

## Statistics

All values describe recorded application-observed wake-up lateness, in nanoseconds:

- `count`, `minimum_ns`, `maximum_ns`, `mean_ns`, and `median_ns` describe the retained observations.
- `sample_stddev_ns` uses the sample standard deviation, with denominator `n - 1`, via `statistics.stdev`.
- `p95_ns` and `p99_ns` use inclusive linear interpolation, commonly called type 7. For sorted values `x[0] ... x[n-1]` and percentile fraction `p`, let `h = (n-1)*p`, `j = floor(h)`, and `f = h-j`. The result is `x[j] + f*(x[j+1]-x[j])`, or `x[j]` at an exact index. Index arithmetic uses integers; the interpolated result can be fractional.

For a header-only dataset, count is zero and all latency statistics are unavailable (`null` in JSON); an empty file is invalid. For one observation, min/max/mean/median/P95/P99 equal that value; sample standard deviation is unavailable. Integer min/max remain exact. Floating-point summary values can round, especially near 64-bit bounds; percentile fractions do not imply additional measurement precision. Sample count and empirical percentiles do not establish worst-case bounds or nanosecond accuracy.

`observed_skipped_periods` is the exact sum of the recorded skipped-period fields, reported separately from latency statistics. It counts omitted grid slots through the last recorded deadline. It excludes slots after that deadline and cannot establish an overall deadline-miss rate or real-time schedulability guarantee.

Validation and summarization retain observations in memory; their memory use grows with the number of rows. Statistics sort the validated latencies. This implementation prioritizes a small, reusable pipeline over streaming percentile estimation.
