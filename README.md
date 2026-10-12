# linux-wakeup-latency-benchmark

A small toolkit for measuring **application-observed wake-up lateness** on 64-bit Linux. A C collector schedules absolute sleeps, records the resulting observations in a versioned CSV, and preserves skipped schedule periods. A Python package validates the CSV, calculates descriptive statistics, and optionally generates plots.

Results describe the observations made by the application. They do not isolate hardware interrupt latency or scheduler dispatch latency, establish worst-case bounds, or provide a real-time schedulability guarantee. Nanosecond units describe the data representation, not measurement accuracy.

Bundled example datasets and test fixtures are synthetic.

## Features and architecture

- One C11 measurement source with checked arguments, timestamp arithmetic, allocation, signals, and output handling.
- `CLOCK_MONOTONIC` and `TIMER_ABSTIME` scheduling that retains the original phase and counts expired slots instead of producing catch-up samples.
- Strict CSV schema-v1 validation and explicit complete, interrupted, error, truncated, and header-only classifications.
- Count, minimum, maximum, mean, median, sample standard deviation, P95, and P99, with recorded skipped periods reported separately.
- Optional headless histogram, empirical cumulative distribution function (ECDF), and latency-versus-sample plots.
- Deterministic fixtures, fault-injection tests, Linux integration tests, and GCC/Clang CI without latency thresholds.

The collector performs measurement; the Python package reads its output afterward. Validation, statistics, and visualization share one CSV validation pipeline. Each CSV is an independent run; observations from separate files are not pooled.

```text
.
├── src/latency_test.c          # C collector
├── wakeup_latency/            # Validation, statistics, plotting, CLI
├── tests/                     # C/Python tests and synthetic CSV fixtures
├── docs/
│   ├── csv-format.md           # CSV schema v1 and measurement semantics
│   ├── analysis.md             # Validation, classifications, statistics
│   └── visualization.md        # Plotting and range accounting
├── .github/workflows/ci.yml   # Linux compatibility and integration checks
├── Makefile
└── requirements-plot.txt      # Optional Matplotlib dependency
```

## Requirements, build, and installation

The collector requires 64-bit Linux, a C11 compiler, and GNU Make. It has no Python runtime dependency and requires no elevated privileges. The analysis package requires Python 3.10 or newer and uses the standard library for validation and statistics; these commands can run independently of the Linux collector. Plotting additionally requires Matplotlib 3.7 or newer, below version 4, and its dependencies. Plotting supports Linux because its file protection uses Linux/POSIX file-descriptor operations.

Python 3.10 is the minimum supported version. The CI workflow is configured for Python 3.10, 3.11, and 3.12; this does not imply that every newer Python version or every 64-bit Linux architecture has been tested. Local platform validation covers x86-64 Linux. See [testing and CI](#testing-and-ci) for the configured checks.

Run the following commands from the repository root:

```sh
make
./build/wakeup-latency --help
python3 -m wakeup_latency --help
```

The default build enables strict warnings, including `-Werror`. To select Clang or rebuild with another compiler:

```sh
make clean
make CC=clang
```

The collector can be installed into a user-owned directory:

```sh
install -Dm755 build/wakeup-latency "$HOME/.local/bin/wakeup-latency"
```

Add that directory to your `PATH` if needed. Run the Python package from the checkout; no package installation is required.

For optional plotting, create a virtual environment and install only the plotting requirements:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-plot.txt
```

CSV validation and statistical analysis work without that installation.

## Collect measurements

```sh
./build/wakeup-latency --interval-us 1000 --samples 1000 --output build/run.csv
```

This requests 1,000 observations at a 1,000-microsecond interval. Defaults are `--interval-us 10000`, `--samples 1000`, and `--output latency.csv`. Options take separate arguments. Interval and sample count accept positive decimal ASCII integers; malformed values, unsupported ranges, duplicate options, and positional arguments are rejected. `--help` is used alone.

Existing output paths are protected by default. Use `--overwrite` explicitly to replace an existing regular file:

```sh
./build/wakeup-latency --interval-us 1000 --samples 1000 --output build/run.csv --overwrite
```

Only regular-file output is supported. Final-component symlinks, devices, and FIFOs are rejected; parent-directory symlinks are permitted. The file is opened before measurement and written after collection. Interrupted or failed runs can leave partial or header-only CSVs. Checked writes, flushes, and closes detect output errors, but they do not provide atomic replacement or `fsync()` durability.

`SIGINT` and `SIGTERM` request cancellation and preserve completed observations. Record the collector's exit status and stderr together with each CSV: the file alone cannot prove successful process completion.

| Collector exit status | Meaning |
| --- | --- |
| `0` | Requested observations completed and CSV output succeeded. |
| `2` | Invalid arguments or unsupported numeric range. |
| `3` | Setup failure, such as allocation or signal-handler setup. |
| `4` | Clock, sleep, or measurement-arithmetic failure. |
| `5` | Output open, write, flush, or close failure. |
| `130` / `143` | Interrupted by SIGINT / SIGTERM. |

An output error takes precedence over measurement or cancellation status.

## Validate, summarize, and plot

```sh
python3 -m wakeup_latency validate build/run.csv
python3 -m wakeup_latency summarize build/run.csv --json
```

Both commands accept multiple CSV paths and report each separately. The validator requires the exact header and version, supported repeated metadata, valid integer ranges, sequential samples, consistent timestamps and latency, and correct deadline/skipped-period progression. Malformed input is rejected with filenames and physical row numbers; invalid rows are never silently removed.

Statistics use recorded latency in nanoseconds. Sample standard deviation uses denominator `n - 1`; P95 and P99 use inclusive linear interpolation (type 7). Header-only runs have unavailable latency statistics; an empty file is invalid. For one observation, all latency statistics except sample standard deviation equal the observed value. Floating-point summaries can round. See [analysis definitions](docs/analysis.md) for the exact rules and completion limits.

After installing the optional plotting dependency:

```sh
python3 -m wakeup_latency plot build/run.csv --output-dir build/plots/run
python3 -m wakeup_latency plot build/run.csv --output-dir build/plots/run-zoom \
    --bins 40 --latency-range-ns 0 100000
```

Each command writes `histogram.png`, `ecdf.png`, and `latency-vs-sample.png` using a headless backend. The optional inclusive range zooms the histogram and ECDF and reports shown, below-range, and above-range observation counts. The ECDF retains all observations in its denominator; the sample plot always shows all observations. Incomplete runs are labeled in every figure. Existing plot files require explicit `--overwrite`. Add `--json` for a machine-readable plot report. See [visualization behavior](docs/visualization.md).

| Analysis/plot exit status | Meaning |
| --- | --- |
| `0` | All inputs classify as complete and requested output succeeded. |
| `1` | Structurally valid input is interrupted, error, truncated, or header-only. Plotting requires at least one observation, so header-only plotting returns `2`. |
| `2` | Invalid/unreadable input, invalid arguments, unavailable plotting dependency, or output failure. |

Interrupted and error classifications remain explicit even when their observed count equals the requested count. A valid prefix declaring `complete` is classified `truncated` if rows are missing. Header-only files contain no recoverable run metadata. A malformed final row is rejected rather than summarized as a prefix.

## Synthetic examples

These commands exercise the package without collecting machine measurements:

```sh
python3 -m wakeup_latency validate tests/fixtures/schema-v1/complete.csv
python3 -m wakeup_latency summarize tests/fixtures/schema-v1/complete.csv --json
python3 -m wakeup_latency plot tests/fixtures/schema-v1/complete.csv \
    --output-dir build/plots/synthetic
python3 -m wakeup_latency plot tests/fixtures/schema-v1/complete.csv \
    --output-dir build/plots/synthetic-zoom --latency-range-ns 0 30
```

The complete fixture contains lateness values `[0, 10, 20, 30, 100]` ns and three recorded skipped periods. Its mean is 32 ns, median 20 ns, P95 86 ns, and P99 97.2 ns. Zooming to 0–30 ns shows four observations and identifies one above the displayed range. These invented values are parser and visualization examples, not representative accuracy or performance claims. Other [fixtures](tests/fixtures/schema-v1/README.md) exercise partial runs and intentionally malformed records.

## CSV schema v1

```csv
schema_version,run_status,requested_samples,sample,interval_ns,deadline_ns,observed_ns,latency_ns,skipped_periods
```

Each completed observation supplies one row. Schema version, terminal collector status, requested count, and interval are repeated. Samples start at 1. `deadline_ns` and `observed_ns` are relative to the same initial monotonic reference `T0`; `latency_ns = observed_ns - deadline_ns` is nonnegative.

For interval `P`, deadline `D_i`, skipped-period count `S_i`, and implicit `D_0 = 0`:

```text
D_i - D_(i-1) = (S_i + 1) * P
```

`S_i` counts expired grid slots skipped **before row i's deadline**. It can be nonzero in the first row. Skips discovered after an observation belong to the next row; periods after the last recorded deadline are not counted. The reported sum is therefore an observation of omitted slots, not an overall deadline-miss rate. See [the complete CSV contract](docs/csv-format.md).

## Testing and CI

```sh
make test
make sanitize
make test-analysis
make test-pipeline
make test-plots
```

`make test` runs C unit tests, collector integration tests, Python analysis tests, a real collector-to-analysis round trip, and plotting tests. Install the optional plotting dependency to run the rendering checks; those checks are explicitly skipped when Matplotlib is unavailable, while standard-library checks still run. Sanitizer checks rebuild the C tests and collector with AddressSanitizer and UndefinedBehaviorSanitizer and run the same suite. Generated files remain under ignored build directories or temporary test directories.

CI configures three Linux jobs: GCC/Python 3.10, Clang/Python 3.11, and GCC/Python 3.12. Rendering and sanitizer checks run in the Python 3.12 job; the other jobs check operation without Matplotlib. Tests check structural correctness, deterministic statistics, explicit range accounting, and successful output; they impose no maximum scheduling latency or real-time performance threshold. Actual allocation and signal-handler setup failures are not fault-injected.

## Measurement methodology and limitations

The collector preallocates and touches storage before taking `T0`, then uses `CLOCK_MONOTONIC` for both absolute scheduling and observations after `clock_nanosleep()`. Before each new wait it skips deadlines at or before a fresh clock reading, preserving the original phase. `EINTR` retries retain the selected deadline unless cancellation was requested. A selected deadline can pass between the clock check and the sleep call; the resulting lateness is recorded.

Cancellation has a small check-to-sleep race: a signal just before the sleep call can delay cancellation until the selected deadline, usually within one interval plus scheduling delay. Another cancellation signal can interrupt the pending sleep. Terminal run status is fixed when collection returns; later cancellation during serialization does not change it.

Observations include timer handling, scheduling, return from sleep, and execution before the timestamp. The collector inherits CPU affinity, scheduling policy, timer slack, system load, and power-management conditions without collecting machine identifiers. `CLOCK_MONOTONIC` excludes suspended time on Linux. Storage is capped at 256 MiB, and absolute timestamp arithmetic must fit signed 64-bit nanoseconds, approximately 292 years from the clock's origin.

CSV schema v1 has no completed-count footer: lost trailing rows in interrupted/error output cannot always be detected. Even a full complete CSV cannot prove a successful collector close. Analysis retains observations in memory, and plots/statistical summaries use floating-point representations that can round large integers. Output protection does not prevent another process from renaming paths or writing concurrently. Keep exit status and diagnostics, inspect tails rather than only zoomed plots, and interpret each result as a descriptive sample of the conditions under which it was collected.
