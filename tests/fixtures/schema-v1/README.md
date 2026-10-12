# Synthetic schema-v1 fixtures

All records in this directory are invented for deterministic tests. They contain no original measurements or machine metadata.

`complete.csv` has lateness values `[0, 10, 20, 30, 100]` ns and three observed skipped periods. Its mean is 32 ns, median 20 ns, sample standard deviation `sqrt(1570)` ns, P95 86 ns, and P99 97.2 ns using inclusive linear interpolation.

The interrupted/error fixtures retain their declared status even when their row count equals the requested count. `truncated.csv` is a valid prefix declaring `complete` but containing fewer rows than requested. `header-only.csv` has no recoverable run metadata. `complete-no-final-newline.csv` intentionally lacks a terminal newline; a complete CSV record does not require one.

Files named for invalid properties are intentionally malformed. `torn-row.csv` ends in a partial record and `blank-row.csv` contains a blank CSV row. Runtime-generated boundary cases supplement these fixtures in `tests/test_analysis.py`.
