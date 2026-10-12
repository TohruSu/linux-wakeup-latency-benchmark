# Descriptive latency visualization

The plotting command loads CSV schema v1 through the same strict validator used for statistics. It produces a histogram, an empirical cumulative distribution function (ECDF), and a latency-versus-sample plot for one run. Matplotlib is imported only when plotting is requested; validation and summary commands use the Python standard library alone.

## Installation and commands

From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-plot.txt
python -m wakeup_latency plot tests/fixtures/schema-v1/complete.csv \
    --output-dir build/plots/synthetic
```

The optional requirement is `matplotlib>=3.7,<4`, including its normal dependencies. Plotting supports Linux, where its output protection uses Linux/POSIX directory and file-descriptor operations. Standard-library validation and statistics remain independent of the Linux collector. Plotting uses the noninteractive Agg backend and requires no display server. The output directory is created if needed. Three PNG files are written:

| File | Axes and meaning |
| --- | --- |
| `histogram.png` | Application-observed wake-up lateness in ns versus observation count per bin. |
| `ecdf.png` | Lateness in ns versus the fraction of all recorded observations at or below that value. |
| `latency-vs-sample.png` | Completed-observation index versus lateness in ns, retaining the original row order. |

`--bins N` controls the histogram bin count and accepts integers from 1 through 10000; the default is 50. Use a separate output directory for each input run. The CLI accepts one input file per plotting invocation rather than combining unrelated observations. Add `--json` to emit a report list containing classification, observation/range counts, selected range, and output paths.

When running with a read-only home directory, set writable cache locations to avoid Matplotlib/font-cache warnings:

```sh
mkdir -p build/matplotlib-cache build/font-cache
MPLCONFIGDIR="$PWD/build/matplotlib-cache" XDG_CACHE_HOME="$PWD/build/font-cache" \
    python3 -m wakeup_latency plot tests/fixtures/schema-v1/complete.csv \
    --output-dir build/plots/synthetic-cache
```

## Distribution zoom and tail accounting

```sh
python -m wakeup_latency plot tests/fixtures/schema-v1/complete.csv \
    --output-dir build/plots/synthetic-zoom --bins 10 --latency-range-ns 0 30
```

The range consists of two integer nanosecond bounds satisfying `0 <= MIN < MAX <= 9223372036854775807`. Both endpoints are inclusive when classifying observations. The histogram displays observations within that range and explicitly reports the total count, shown count, count below the range, and count above it. Range selection changes the display, not validation or statistics.

The ECDF is defined as:

```text
F_n(x) = number of recorded latencies <= x / total recorded observations
```

Its x-axis can be zoomed to the requested range, but its denominator remains the total number of recorded observations. It is not renormalized to the visible subset. When the range excludes a lower or upper tail, the visible curve therefore need not start at zero or reach one. Points at the range boundaries mark the inclusive ECDF values so a jump at an axis edge remains visible. The sample plot always retains the entire observed sequence and is not zoomed by this distribution-range option.

The synthetic fixture has `[0, 10, 20, 30, 100]` ns. The inclusive range 0–30 ns shows four observations, none below, and one above. The ECDF at 30 ns is `4 / 5 = 0.8`, rather than 1. Counts remain explicit even when no observations lie inside a requested range.

## Validation, run status, and errors

Plotting rejects invalid/missing fields, unsupported headers or versions, non-integer and nonfinite values, inconsistent timestamp arithmetic, and invalid deadline/skipped-period progression through the existing validation pipeline. It does not coerce invalid values or silently drop rows. CSV diagnostics include the input filename and physical record-start line.

Every figure identifies the input file, classification, observed/requested counts, and recorded skipped periods. Valid interrupted, error, and truncated datasets can be plotted, with their classification retained. These figures describe the available observations and do not imply successful collection or recover lost rows. Header-only runs cannot be plotted because no observations or run metadata are available.

| Exit status | Meaning |
| --- | --- |
| `0` | A valid complete run was plotted successfully. |
| `1` | A valid interrupted, error, or truncated run was plotted successfully. |
| `2` | Invalid/unreadable input, header-only input, invalid arguments, missing Matplotlib, or output failure. |

No maximum acceptable latency is applied. Statistics are unchanged by plotting, and the figures are descriptive rather than real-time performance guarantees. Nanosecond labels do not establish nanosecond accuracy. Range membership and histogram counts use exact integer arithmetic, while rendering uses floating-point coordinates and can round integer timestamps near their upper bounds. An otherwise valid range is rejected if its bin boundaries collapse at floating-point precision; use wider bounds or fewer bins. Default display bounds are padded for constant or very narrow distributions without adding or removing observations.

## Output protection

Existing destination files are rejected by default. Rerunning with the same output directory requires an explicit option:

```sh
python -m wakeup_latency plot tests/fixtures/schema-v1/complete.csv \
    --output-dir build/plots/synthetic --overwrite
```

Overwrite is restricted to regular files. Final-component file symlinks and nonregular outputs, including devices and FIFOs, are rejected. A final-component symlink used as the output directory is also rejected; symlinks in parent directories are permitted. The implementation anchors output operations to the opened directory and writes through checked file descriptors.

The three-file output is not an atomic transaction. A write/close failure can leave a partial set or an incomplete PNG, and no `fsync()` durability guarantee is provided. Concurrent writes, path renames, and replacement by other processes are not prevented. Use a dedicated output directory and inspect the command's exit status.

## Verification

```sh
make test-plots
```

Plot tests use synthetic fixtures and exercise headless generation, range counts, ECDF behavior, classifications, invalid input, and output failures. They check data and structural behavior rather than comparing screenshots or imposing timing-performance thresholds. Without Matplotlib, rendering checks are explicitly skipped; the tests for standard-library operation, invalid input, and missing-dependency diagnostics still run. Install the optional requirements to execute the full plotting suite.
