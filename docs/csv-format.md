# CSV schema version 1

The collector writes one header and one row per completed observation. All numeric fields are base-10 integers. The schema contains run status, schedule configuration, and observations, without device-identifying metadata.

```csv
schema_version,run_status,requested_samples,sample,interval_ns,deadline_ns,observed_ns,latency_ns,skipped_periods
```

| Field | Meaning |
| --- | --- |
| `schema_version` | `1`, repeated in every row. |
| `run_status` | Collector status fixed when collection returns: `complete`, `interrupted`, or `error`, repeated in every row. An output failure is additionally signaled by exit status 5. |
| `requested_samples` | Original requested observation count, repeated in every row. |
| `sample` | Completed-observation index, starting at 1 and increasing without gaps. |
| `interval_ns` | Positive configured interval in integer nanoseconds, repeated in every row. |
| `deadline_ns` | Absolute sleep deadline expressed relative to the run's `T0`. |
| `observed_ns` | Timestamp taken after the sleep returns, expressed relative to the same `T0`. |
| `latency_ns` | `observed_ns - deadline_ns`, required to be nonnegative. |
| `skipped_periods` | Number of expired interval-grid slots omitted **before this observation's deadline**. |

`T0` is a `CLOCK_MONOTONIC` reading taken after storage preparation. Both timestamps use the same clock. Relative values avoid publishing a machine's monotonic uptime. They are elapsed-clock values, not wall-clock dates. Nanosecond units specify representation, not accuracy.

For interval `P`, deadlines are selected from `T0 + k*P` with positive integer `k`. Before each new sleep, a fresh clock reading is used to skip candidate deadlines at or before that reading. The phase remains fixed at `T0`; it is never reset to the latest observation. A deadline can still pass between the check and the sleep call, in which case the resulting lateness is recorded normally. `EINTR` retries retain the selected deadline rather than selecting a new one.

Let `D_i` be row `i`'s `deadline_ns`, `S_i` its `skipped_periods`, and `D_0=0`. Every completed row satisfies:

```text
D_i - D_(i-1) = (S_i + 1) * P
latency_ns = observed_ns - deadline_ns >= 0
```

The first row can have skipped periods if setup after `T0` or scheduling delays prevent the first grid slot from being used. Skips discovered after a late observation belong to the **next** recorded row. No future slots are counted after the final sample or cancellation. Consequently, summing `skipped_periods` counts omitted slots through the last recorded deadline, not all slots elapsed by the end of the process.

Completed runs have exactly `requested_samples` rows and status `complete`. Interrupted and measurement-error runs retain the completed valid rows with the corresponding status. A SIGINT or SIGTERM received during later serialization does not change the collector's terminal status. If no samples completed, the CSV is header-only: it cannot encode run metadata or status. Use the process exit status and standard-error diagnostic to determine that outcome. Output failures can truncate a row or the file, so a nonzero output status must not be treated as a valid complete dataset. Only regular-file output is supported; successful write/flush/close checks do not guarantee persistence after a system crash.

No metadata sidecar is produced. Consumers should require the exact header and schema version, validate integer ranges and repeated fields, and verify sample order, deadline progression, and the latency equation before computing statistics.
