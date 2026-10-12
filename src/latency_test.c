/* Application-observed wake-up lateness on 64-bit Linux. */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifndef __linux__
#error "This collector supports Linux only"
#endif
_Static_assert(sizeof(void *) == 8, "A 64-bit Linux ABI is required");
_Static_assert(sizeof(time_t) == 8, "64-bit time_t is required");
_Static_assert(sizeof(size_t) == 8, "64-bit size_t is required");

#define NS_PER_SECOND INT64_C(1000000000)
#define NS_PER_MICROSECOND INT64_C(1000)
#define MAX_SAMPLE_BYTES (UINT64_C(256) * UINT64_C(1024) * UINT64_C(1024))

enum exit_code {
    EXIT_ARGUMENT = 2,
    EXIT_SETUP = 3,
    EXIT_MEASUREMENT = 4,
    EXIT_OUTPUT = 5
};
enum run_status { RUN_COMPLETE, RUN_INTERRUPTED, RUN_ERROR };

struct config {
    int64_t interval_ns;
    size_t count;
    const char *output_path;
    bool overwrite;
};
struct sample {
    int64_t deadline_ns;
    int64_t observed_ns;
    int64_t latency_ns;
    uint64_t skipped_periods;
};
struct run_result {
    size_t completed;
    enum run_status status;
    int error_code;
    const char *error_operation;
    int signal_number;
};
/* Both operations return zero or a positive errno value. */
struct clock_ops {
    int (*now)(struct timespec *stamp);
    int (*sleep_until)(const struct timespec *deadline);
};

static volatile sig_atomic_t stop_signal;

static int checked_add(int64_t left, int64_t right, int64_t *result)
{
    if ((right > 0 && left > INT64_MAX - right) ||
        (right < 0 && left < INT64_MIN - right)) {
        return EOVERFLOW;
    }
    *result = left + right;
    return 0;
}

static int checked_subtract(int64_t left, int64_t right, int64_t *result)
{
    if ((right > 0 && left < INT64_MIN + right) ||
        (right < 0 && left > INT64_MAX + right)) {
        return EOVERFLOW;
    }
    *result = left - right;
    return 0;
}

/* Restrict absolute monotonic timestamps to signed 64-bit nanoseconds. */
static int timespec_to_ns(const struct timespec *stamp, int64_t *result)
{
    if (stamp->tv_sec < 0 || stamp->tv_nsec < 0 ||
        stamp->tv_nsec >= NS_PER_SECOND) {
        return EINVAL;
    }
    if (stamp->tv_sec > INT64_MAX / NS_PER_SECOND) {
        return EOVERFLOW;
    }
    return checked_add((int64_t)stamp->tv_sec * NS_PER_SECOND,
                       (int64_t)stamp->tv_nsec, result);
}

static int ns_to_timespec(int64_t ns, struct timespec *result)
{
    if (ns < 0) {
        return EINVAL;
    }
    result->tv_sec = (time_t)(ns / NS_PER_SECOND);
    result->tv_nsec = (long)(ns % NS_PER_SECOND);
    return 0;
}

static int parse_positive_u64(const char *text, uint64_t *result)
{
    uint64_t value = 0;
    if (*text == '\0') {
        return EINVAL;
    }
    for (const unsigned char *p = (const unsigned char *)text; *p != 0; ++p) {
        if (*p < (unsigned char)'0' || *p > (unsigned char)'9') {
            return EINVAL;
        }
        uint64_t digit = (uint64_t)(*p - (unsigned char)'0');
        if (value > (UINT64_MAX - digit) / UINT64_C(10)) {
            return ERANGE;
        }
        value = value * UINT64_C(10) + digit;
    }
    if (value == 0) {
        return EINVAL;
    }
    *result = value;
    return 0;
}

static int validate_config(const struct config *cfg)
{
    if (cfg->interval_ns <= 0 || cfg->count == 0 ||
        cfg->output_path == NULL || cfg->output_path[0] == '\0') {
        return EINVAL;
    }
    if (cfg->count > SIZE_MAX / sizeof(struct sample) ||
        (uint64_t)cfg->count > (uint64_t)(INT64_MAX / cfg->interval_ns)) {
        return EOVERFLOW;
    }
    if ((uint64_t)cfg->count * sizeof(struct sample) > MAX_SAMPLE_BYTES) {
        return E2BIG;
    }
    return 0;
}

/* Deliberately accept only unambiguous, space-separated options. */
static int parse_config(int argc, char *argv[], struct config *cfg,
                        const char **error)
{
    bool interval_seen = false;
    bool count_seen = false;
    bool output_seen = false;
    *cfg = (struct config){ INT64_C(10000000), 1000, "latency.csv", false };
    *error = NULL;
    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        return 1;
    }
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--overwrite") == 0) {
            if (cfg->overwrite) {
                *error = "duplicate --overwrite";
                return -1;
            }
            cfg->overwrite = true;
            continue;
        }
        bool is_interval = strcmp(argv[i], "--interval-us") == 0;
        bool is_count = strcmp(argv[i], "--samples") == 0;
        bool is_output = strcmp(argv[i], "--output") == 0;
        if (!is_interval && !is_count && !is_output) {
            *error = "unknown option or positional argument (see --help)";
            return -1;
        }
        if (i + 1 >= argc) {
            *error = "missing option value";
            return -1;
        }
        const char *value = argv[++i];
        uint64_t number = 0;
        if (is_interval) {
            if (interval_seen || parse_positive_u64(value, &number) != 0 ||
                number > (uint64_t)(INT64_MAX / NS_PER_MICROSECOND)) {
                *error = "--interval-us requires a positive integer in range, once";
                return -1;
            }
            interval_seen = true;
            cfg->interval_ns = (int64_t)number * NS_PER_MICROSECOND;
        } else if (is_count) {
            if (count_seen || parse_positive_u64(value, &number) != 0 ||
                number > SIZE_MAX) {
                *error = "--samples requires a positive integer in range, once";
                return -1;
            }
            count_seen = true;
            cfg->count = (size_t)number;
        } else {
            if (output_seen || value[0] == '\0') {
                *error = "--output requires a nonempty path, once";
                return -1;
            }
            output_seen = true;
            cfg->output_path = value;
        }
    }
    int ret = validate_config(cfg);
    if (ret != 0) {
        *error = ret == E2BIG ? "sample storage exceeds the 256 MiB limit" :
                               "sample count or nominal run duration is out of range";
        return -1;
    }
    return 0;
}

/* Choose the first strictly future slot on the existing phase grid. */
static int skip_expired(int64_t candidate, int64_t now, int64_t interval,
                        int64_t *future, uint64_t *skipped)
{
    if (candidate < 0 || now < 0 || interval <= 0) {
        return EINVAL;
    }
    *skipped = 0;
    *future = candidate;
    if (now < candidate) {
        return 0;
    }
    uint64_t slots = (uint64_t)((now - candidate) / interval) + UINT64_C(1);
    if (slots > (uint64_t)(INT64_MAX / interval)) {
        return EOVERFLOW;
    }
    int ret = checked_add(candidate, (int64_t)slots * interval, future);
    if (ret == 0) {
        *skipped = slots;
    }
    return ret;
}

static int real_now(struct timespec *stamp)
{
    if (clock_gettime(CLOCK_MONOTONIC, stamp) != 0) {
        return errno != 0 ? errno : EIO;
    }
    return 0;
}

static int real_sleep_until(const struct timespec *deadline)
{
    return clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, deadline, NULL);
}

static int read_now(const struct clock_ops *ops, int64_t *now)
{
    struct timespec stamp;
    int ret = ops->now(&stamp);
    return ret != 0 ? ret : timespec_to_ns(&stamp, now);
}

static void measurement_error(struct run_result *run, const char *operation,
                              int error_code)
{
    run->status = RUN_ERROR;
    run->error_operation = operation;
    run->error_code = error_code;
}

static bool cancellation_requested(struct run_result *run)
{
    if (stop_signal == 0) {
        return false;
    }
    run->status = RUN_INTERRUPTED;
    run->signal_number = (int)stop_signal;
    return true;
}

static void collect_samples(const struct config *cfg, struct sample *samples,
                            struct run_result *run, const struct clock_ops *ops)
{
    *run = (struct run_result){ 0, RUN_COMPLETE, 0, NULL, 0 };
    if (cancellation_requested(run)) {
        return;
    }
    int64_t origin = 0;
    int ret = read_now(ops, &origin);
    if (ret != 0) {
        measurement_error(run, "initial clock read", ret);
        return;
    }
    int64_t candidate = 0;
    ret = checked_add(origin, cfg->interval_ns, &candidate);
    if (ret != 0) {
        measurement_error(run, "initial deadline arithmetic", ret);
        return;
    }
    int64_t previous_clock = origin;
    while (run->completed < cfg->count) {
        if (cancellation_requested(run)) {
            return;
        }
        int64_t before_wait = 0;
        ret = read_now(ops, &before_wait);
        if (ret != 0 || before_wait < previous_clock) {
            measurement_error(run, "pre-sleep clock read", ret != 0 ? ret : ERANGE);
            return;
        }
        int64_t deadline = 0;
        uint64_t skipped = 0;
        ret = skip_expired(candidate, before_wait, cfg->interval_ns, &deadline, &skipped);
        if (ret != 0) {
            measurement_error(run, "skipped-deadline arithmetic", ret);
            return;
        }
        struct timespec sleep_deadline;
        ret = ns_to_timespec(deadline, &sleep_deadline);
        if (ret != 0) {
            measurement_error(run, "deadline conversion", ret);
            return;
        }
        /* Signals cannot be atomically unmasked with clock_nanosleep.
         * A signal in the final check-to-sleep gap can delay cancellation until
         * this deadline or another signal; see the documented limitation.
         */
        do {
            if (cancellation_requested(run)) {
                return;
            }
            ret = ops->sleep_until(&sleep_deadline);
        } while (ret == EINTR);
        if (ret != 0) {
            measurement_error(run, "clock_nanosleep", ret);
            return;
        }
        int64_t observed = 0;
        ret = read_now(ops, &observed);
        if (ret != 0 || observed < before_wait || observed < deadline) {
            measurement_error(run, "post-sleep clock read", ret != 0 ? ret : ERANGE);
            return;
        }
        struct sample row;
        ret = checked_subtract(deadline, origin, &row.deadline_ns);
        if (ret == 0) {
            ret = checked_subtract(observed, origin, &row.observed_ns);
        }
        if (ret == 0) {
            ret = checked_subtract(observed, deadline, &row.latency_ns);
        }
        if (ret != 0) {
            measurement_error(run, "sample timestamp arithmetic", ret);
            return;
        }
        row.skipped_periods = skipped;
        samples[run->completed++] = row;
        previous_clock = observed;
        if (cancellation_requested(run) || run->completed == cfg->count) {
            return;
        }
        ret = checked_add(deadline, cfg->interval_ns, &candidate);
        if (ret != 0) {
            measurement_error(run, "next deadline arithmetic", ret);
            return;
        }
    }
}

static const char *status_name(enum run_status status)
{
    switch (status) {
    case RUN_COMPLETE: return "complete";
    case RUN_INTERRUPTED: return "interrupted";
    case RUN_ERROR: return "error";
    }
    return "error";
}

/* The caller must also check fclose. Always inspect sticky stream errors. */
static int write_csv(FILE *stream, const struct config *cfg,
                     const struct sample *samples, const struct run_result *run)
{
    if (fprintf(stream, "schema_version,run_status,requested_samples,sample,"
                        "interval_ns,deadline_ns,observed_ns,latency_ns,"
                        "skipped_periods\n") < 0) {
        return errno != 0 ? errno : EIO;
    }
    for (size_t i = 0; i < run->completed; ++i) {
        const struct sample *row = &samples[i];
        if (fprintf(stream, "1,%s,%zu,%zu,%" PRId64 ",%" PRId64 ",%" PRId64
                            ",%" PRId64 ",%" PRIu64 "\n",
                    status_name(run->status), cfg->count, i + 1, cfg->interval_ns,
                    row->deadline_ns, row->observed_ns, row->latency_ns,
                    row->skipped_periods) < 0) {
            return errno != 0 ? errno : EIO;
        }
    }
    if (fflush(stream) != 0 || ferror(stream)) {
        return errno != 0 ? errno : EIO;
    }
    return 0;
}

/* Close even after a write/flush failure, preserving the first failure. */
static int finish_output(FILE *stream, const struct config *cfg,
                         const struct sample *samples, const struct run_result *run)
{
    int error_code = write_csv(stream, cfg, samples, run);
    if (fclose(stream) != 0 && error_code == 0) {
        error_code = errno != 0 ? errno : EIO;
    }
    return error_code;
}

static void handle_stop(int signal_number)
{
    if (stop_signal == 0) {
        stop_signal = (sig_atomic_t)signal_number;
    }
}

static int install_stop_handlers(struct sigaction *old_int, struct sigaction *old_term)
{
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = handle_stop;
    /* Prevent the other cancellation handler from interrupting the first
     * handler between its flag check and store.
     */
    if (sigemptyset(&action.sa_mask) != 0 ||
        sigaddset(&action.sa_mask, SIGINT) != 0 ||
        sigaddset(&action.sa_mask, SIGTERM) != 0 ||
        sigaction(SIGINT, &action, old_int) != 0) {
        return errno;
    }
    if (sigaction(SIGTERM, &action, old_term) != 0) {
        int error_code = errno;
        (void)sigaction(SIGINT, old_int, NULL);
        return error_code;
    }
    return 0;
}

static void usage(FILE *stream, const char *program)
{
    (void)fprintf(stream,
        "Usage: %s [--interval-us N] [--samples N] [--output PATH] [--overwrite]\n"
        "       %s --help\n"
        "Defaults: 10000 microseconds, 1000 samples, latency.csv.\n"
        "Values are positive decimal integers; sample storage is limited to 256 MiB.\n"
        "Existing paths are protected unless --overwrite is given; symlinks are rejected.\n"
        "Measures application-observed wake-up lateness; ns units do not imply ns accuracy.\n",
        program, program);
}

int main(int argc, char *argv[])
{
    struct config cfg;
    const char *argument_error = NULL;
    int ret = parse_config(argc, argv, &cfg, &argument_error);
    if (ret == 1) {
        usage(stdout, argv[0]);
        return fflush(stdout) == 0 && !ferror(stdout) ? 0 : EXIT_OUTPUT;
    }
    if (ret != 0) {
        (void)fprintf(stderr, "Argument error: %s\n", argument_error);
        usage(stderr, argv[0]);
        return EXIT_ARGUMENT;
    }
    struct sigaction old_int;
    struct sigaction old_term;
    stop_signal = 0;
    ret = install_stop_handlers(&old_int, &old_term);
    if (ret != 0) {
        (void)fprintf(stderr, "Signal handler setup failed: %s\n", strerror(ret));
        return EXIT_SETUP;
    }
    int exit_status = EXIT_SETUP;
    size_t bytes = cfg.count * sizeof(struct sample);
    struct sample *samples = malloc(bytes);
    if (samples == NULL) {
        (void)fprintf(stderr, "Allocation failed: %s\n", strerror(errno));
        goto cleanup;
    }
    /* Observable stores ensure pages are touched before T0, including at -O2. */
    volatile struct sample *touch = samples;
    for (size_t i = 0; i < cfg.count && stop_signal == 0; ++i) {
        touch[i].deadline_ns = 0;
        touch[i].observed_ns = 0;
        touch[i].latency_ns = 0;
        touch[i].skipped_periods = 0;
    }
    /* Nonblocking open prevents an existing FIFO from hanging before fstat. */
    int flags = O_WRONLY | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK;
    flags |= cfg.overwrite ? O_TRUNC : O_EXCL;
    int fd = open(cfg.output_path, flags, 0666);
    if (fd < 0) {
        (void)fprintf(stderr, "Cannot open output '%s': %s\n", cfg.output_path, strerror(errno));
        exit_status = EXIT_OUTPUT;
        goto cleanup;
    }
    struct stat output_info;
    if (fstat(fd, &output_info) != 0) {
        int output_error = errno;
        (void)close(fd);
        (void)fprintf(stderr, "Cannot inspect output: %s\n", strerror(output_error));
        exit_status = EXIT_OUTPUT;
        goto cleanup;
    }
    if (!S_ISREG(output_info.st_mode)) {
        (void)close(fd);
        (void)fprintf(stderr, "Output must be a regular file\n");
        exit_status = EXIT_OUTPUT;
        goto cleanup;
    }
    FILE *output = fdopen(fd, "w");
    if (output == NULL) {
        int output_error = errno;
        (void)close(fd);
        (void)fprintf(stderr, "Cannot open output stream: %s\n", strerror(output_error));
        exit_status = EXIT_OUTPUT;
        goto cleanup;
    }
    struct run_result run;
    const struct clock_ops ops = { real_now, real_sleep_until };
    collect_samples(&cfg, samples, &run, &ops);
    exit_status = run.status == RUN_COMPLETE ? 0 :
                  run.status == RUN_INTERRUPTED ? 128 + run.signal_number : EXIT_MEASUREMENT;
    if (run.status == RUN_ERROR) {
        (void)fprintf(stderr, "%s failed: %s\n", run.error_operation, strerror(run.error_code));
    }
    int write_error = finish_output(output, &cfg, samples, &run);
    if (write_error != 0) {
        (void)fprintf(stderr, "Output failed: %s; CSV may be incomplete\n", strerror(write_error));
        exit_status = EXIT_OUTPUT;
    } else {
        (void)fprintf(stderr, "Run %s: %zu/%zu measurements written to %s\n",
                      status_name(run.status), run.completed, cfg.count, cfg.output_path);
    }
cleanup:
    /* Handlers stay active through serialization; terminal status was fixed
     * when collection ended. Setup errors and output errors take precedence.
     */
    (void)sigaction(SIGINT, &old_int, NULL);
    (void)sigaction(SIGTERM, &old_term, NULL);
    free(samples);
    return exit_status;
}
