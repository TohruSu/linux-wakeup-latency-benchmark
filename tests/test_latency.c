#define _GNU_SOURCE
#define main latency_program_main
#include "../src/latency_test.c"
#undef main

#include <limits.h>

static unsigned int test_assertions;

#define CHECK(condition)                                                        \
    do {                                                                        \
        ++test_assertions;                                                      \
        if (!(condition)) {                                                     \
            fprintf(stderr, "%s:%d: check failed: %s\n",                        \
                    __FILE__, __LINE__, #condition);                            \
            exit(EXIT_FAILURE);                                                 \
        }                                                                       \
    } while (0)

struct test_clock_event {
    int error_code;
    int64_t timestamp_ns;
};

struct test_sleep_event {
    int error_code;
    int signal_number;
};

static const struct test_clock_event *test_clock_events;
static size_t test_clock_count;
static size_t test_clock_index;
static const struct test_sleep_event *test_sleep_events;
static size_t test_sleep_count;
static size_t test_sleep_index;
static int64_t test_sleep_deadlines[32];

static int test_now(struct timespec *timestamp)
{
    CHECK(test_clock_index < test_clock_count);
    const struct test_clock_event event = test_clock_events[test_clock_index++];
    if (event.error_code != 0) {
        return event.error_code;
    }
    CHECK(ns_to_timespec(event.timestamp_ns, timestamp) == 0);
    return 0;
}

static int test_sleep_until(const struct timespec *deadline)
{
    CHECK(test_sleep_index < test_sleep_count);
    CHECK(test_sleep_index < sizeof(test_sleep_deadlines) /
                               sizeof(test_sleep_deadlines[0]));
    CHECK(timespec_to_ns(deadline, &test_sleep_deadlines[test_sleep_index]) == 0);
    const struct test_sleep_event event = test_sleep_events[test_sleep_index++];
    if (event.signal_number != 0) {
        stop_signal = event.signal_number;
    }
    return event.error_code;
}

static const struct clock_ops test_ops = {
    .now = test_now,
    .sleep_until = test_sleep_until,
};

static void test_reset_events(const struct test_clock_event *clock_events,
                              size_t clock_count,
                              const struct test_sleep_event *sleep_events,
                              size_t sleep_count)
{
    test_clock_events = clock_events;
    test_clock_count = clock_count;
    test_clock_index = 0;
    test_sleep_events = sleep_events;
    test_sleep_count = sleep_count;
    test_sleep_index = 0;
    memset(test_sleep_deadlines, 0, sizeof(test_sleep_deadlines));
    stop_signal = 0;
}

static struct config test_configuration(size_t count)
{
    const struct config config = {
        .interval_ns = 10,
        .count = count,
        .output_path = "unused.csv",
        .overwrite = false,
    };
    return config;
}

static void test_arithmetic(void)
{
    int64_t result = 0;
    CHECK(checked_add(4, 7, &result) == 0 && result == 11);
    CHECK(checked_add(INT64_MAX, 0, &result) == 0 && result == INT64_MAX);
    CHECK(checked_add(INT64_MIN, 0, &result) == 0 && result == INT64_MIN);
    CHECK(checked_add(INT64_MAX, 1, &result) == EOVERFLOW);
    CHECK(checked_add(INT64_MIN, -1, &result) == EOVERFLOW);
    CHECK(checked_add(INT64_MIN, INT64_MAX, &result) == 0 && result == -1);
    CHECK(checked_subtract(1000000001, 999999999, &result) == 0 && result == 2);
    CHECK(checked_subtract(0, 7, &result) == 0 && result == -7);
    CHECK(checked_subtract(INT64_MIN, INT64_MIN, &result) == 0 && result == 0);
    CHECK(checked_subtract(INT64_MAX, -1, &result) == EOVERFLOW);
    CHECK(checked_subtract(INT64_MIN, 1, &result) == EOVERFLOW);
    CHECK(checked_subtract(0, INT64_MIN, &result) == EOVERFLOW);

    const int64_t values[] = {
        0, 1, 999999999, 1000000000, 1000000001, INT64_MAX,
    };
    for (size_t i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
        struct timespec timestamp;
        CHECK(ns_to_timespec(values[i], &timestamp) == 0);
        CHECK(timestamp.tv_nsec >= 0 && timestamp.tv_nsec < 1000000000L);
        CHECK(timespec_to_ns(&timestamp, &result) == 0 && result == values[i]);
    }
    struct timespec normalized;
    CHECK(ns_to_timespec(1000000001, &normalized) == 0);
    CHECK(normalized.tv_sec == 1 && normalized.tv_nsec == 1);
    CHECK(ns_to_timespec(-1, &normalized) != 0);

    const struct timespec invalid[] = {
        {.tv_sec = -1, .tv_nsec = 0},
        {.tv_sec = 0, .tv_nsec = -1},
        {.tv_sec = 0, .tv_nsec = 1000000000L},
        {.tv_sec = (time_t)(INT64_MAX / 1000000000 + 1), .tv_nsec = 0},
        {.tv_sec = (time_t)(INT64_MAX / 1000000000),
         .tv_nsec = (long)(INT64_MAX % 1000000000 + 1)},
    };
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        CHECK(timespec_to_ns(&invalid[i], &result) != 0);
    }
}

static void test_positive_parser(void)
{
    uint64_t value = 0;
    CHECK(parse_positive_u64("1", &value) == 0 && value == 1);
    CHECK(parse_positive_u64("0010", &value) == 0 && value == 10);
    CHECK(parse_positive_u64("18446744073709551615", &value) == 0 &&
          value == UINT64_MAX);
    const char *invalid[] = {
        "", "0", "-1", "+1", " 1", "1 ", "1x", "1.0", "0x10",
        "18446744073709551616", "999999999999999999999999999999999999",
    };
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        CHECK(parse_positive_u64(invalid[i], &value) != 0);
    }
}

static void test_argument_configuration(void)
{
    struct config config;
    const char *error = NULL;
    char *defaults[] = {"wakeup-latency"};
    CHECK(parse_config(1, defaults, &config, &error) == 0);
    CHECK(config.interval_ns == 10000000 && config.count == 1000);
    CHECK(strcmp(config.output_path, "latency.csv") == 0 && !config.overwrite);

    char *valid[] = {
        "wakeup-latency", "--interval-us", "250", "--samples", "2",
        "--output", "test.csv", "--overwrite",
    };
    CHECK(parse_config(8, valid, &config, &error) == 0);
    CHECK(config.interval_ns == 250000 && config.count == 2);
    CHECK(strcmp(config.output_path, "test.csv") == 0 && config.overwrite);

    char *help[] = {"wakeup-latency", "--help"};
    CHECK(parse_config(2, help, &config, &error) == 1);

    struct test_arguments {
        int argc;
        char *argv[8];
    };
    const struct test_arguments invalid[] = {
        {2, {"wakeup-latency", "--interval-us"}},
        {3, {"wakeup-latency", "--interval-us", "0"}},
        {3, {"wakeup-latency", "--interval-us", "-1"}},
        {3, {"wakeup-latency", "--interval-us", "1.5"}},
        {3, {"wakeup-latency", "--interval-us", "9223372036854776"}},
        {3, {"wakeup-latency", "--samples", "0"}},
        {3, {"wakeup-latency", "--samples", "18446744073709551615"}},
        {3, {"wakeup-latency", "--samples", "18446744073709551616"}},
        {2, {"wakeup-latency", "--output"}},
        {3, {"wakeup-latency", "--output", ""}},
        {2, {"wakeup-latency", "--unknown"}},
        {2, {"wakeup-latency", "1000"}},
        {3, {"wakeup-latency", "--overwrite", "--overwrite"}},
        {5, {"wakeup-latency", "--samples", "1", "--samples", "2"}},
        {5, {"wakeup-latency", "--interval-us", "1", "--interval-us", "2"}},
        {5, {"wakeup-latency", "--output", "a.csv", "--output", "b.csv"}},
        {3, {"wakeup-latency", "--help", "--overwrite"}},
        {3, {"wakeup-latency", "--overwrite", "--help"}},
        {5, {"wakeup-latency", "--interval-us", "9223372036854775",
             "--samples", "2"}},
    };
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        /* The parser does not mutate argument strings. */
        char *arguments[8];
        for (int j = 0; j < invalid[i].argc; ++j) {
            arguments[j] = invalid[i].argv[j];
        }
        error = NULL;
        CHECK(parse_config(invalid[i].argc, arguments, &config, &error) == -1);
        CHECK(error != NULL);
    }

    config = test_configuration(1);
    CHECK(validate_config(&config) == 0);
    config.count = 0;
    CHECK(validate_config(&config) != 0);
    config = test_configuration(1);
    config.interval_ns = 0;
    CHECK(validate_config(&config) != 0);
    config.interval_ns = -1;
    CHECK(validate_config(&config) != 0);
    config = test_configuration(SIZE_MAX);
    CHECK(validate_config(&config) != 0);
    config = test_configuration((size_t)(UINT64_C(256) * 1024 * 1024 /
                                         sizeof(struct sample)) + 1);
    CHECK(validate_config(&config) == E2BIG);
    config = test_configuration(2);
    config.interval_ns = INT64_MAX;
    CHECK(validate_config(&config) != 0);
}

static void test_skipped_periods(void)
{
    int64_t future = 0;
    uint64_t skipped = 99;
    CHECK(skip_expired(10, 9, 10, &future, &skipped) == 0);
    CHECK(future == 10 && skipped == 0);
    CHECK(skip_expired(10, 10, 10, &future, &skipped) == 0);
    CHECK(future == 20 && skipped == 1);
    CHECK(skip_expired(10, 19, 10, &future, &skipped) == 0);
    CHECK(future == 20 && skipped == 1);
    CHECK(skip_expired(10, 20, 10, &future, &skipped) == 0);
    CHECK(future == 30 && skipped == 2);
    CHECK(skip_expired(10, 35, 10, &future, &skipped) == 0);
    CHECK(future == 40 && skipped == 3);
    CHECK(skip_expired(10, 35, 0, &future, &skipped) != 0);
    CHECK(skip_expired(10, 35, -1, &future, &skipped) != 0);
    CHECK(skip_expired(INT64_MAX - 9, INT64_MAX - 5, 10,
                       &future, &skipped) == EOVERFLOW);
    CHECK(skip_expired(INT64_MAX, INT64_MAX, 1,
                       &future, &skipped) == EOVERFLOW);
    CHECK(skip_expired(1, INT64_MAX - 1, 1,
                       &future, &skipped) == 0);
    CHECK(future == INT64_MAX && skipped == (uint64_t)INT64_MAX - 1);
}

static void test_collect_progression(void)
{
    const struct test_clock_event clock_events[] = {
        {0, 1000}, {0, 1000}, {0, 1013},
        {0, 1030}, {0, 1041}, {0, 1041}, {0, 1050},
    };
    const struct test_sleep_event sleep_events[] = {{0, 0}, {0, 0}, {0, 0}};
    test_reset_events(clock_events, 7, sleep_events, 3);
    struct sample samples[3] = {{0}};
    struct run_result run;
    const struct config config = test_configuration(3);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_COMPLETE && run.completed == 3);
    CHECK(test_clock_index == 7 && test_sleep_index == 3);
    CHECK(test_sleep_deadlines[0] == 1010);
    CHECK(test_sleep_deadlines[1] == 1040);
    CHECK(test_sleep_deadlines[2] == 1050);
    CHECK(samples[0].deadline_ns == 10 && samples[0].observed_ns == 13);
    CHECK(samples[0].latency_ns == 3 && samples[0].skipped_periods == 0);
    CHECK(samples[1].deadline_ns == 40 && samples[1].observed_ns == 41);
    CHECK(samples[1].latency_ns == 1 && samples[1].skipped_periods == 2);
    CHECK(samples[2].deadline_ns == 50 && samples[2].observed_ns == 50);
    CHECK(samples[2].latency_ns == 0 && samples[2].skipped_periods == 0);
}

static void test_collect_eintr(void)
{
    const struct test_clock_event clock_events[] = {
        {0, 1000}, {0, 1000}, {0, 1035},
    };
    const struct test_sleep_event sleep_events[] = {
        {EINTR, 0}, {EINTR, 0}, {0, 0},
    };
    test_reset_events(clock_events, 3, sleep_events, 3);
    struct sample sample = {0};
    struct run_result run;
    const struct config config = test_configuration(1);
    collect_samples(&config, &sample, &run, &test_ops);
    CHECK(run.status == RUN_COMPLETE && run.completed == 1);
    CHECK(test_clock_index == 3 && test_sleep_index == 3);
    CHECK(test_sleep_deadlines[0] == 1010);
    CHECK(test_sleep_deadlines[1] == 1010);
    CHECK(test_sleep_deadlines[2] == 1010);
    CHECK(sample.latency_ns == 25 && sample.skipped_periods == 0);
}

static void test_initial_skips(void)
{
    const struct test_clock_event clocks[] = {
        {0, 1000}, {0, 1030}, {0, 1041},
    };
    const struct test_sleep_event sleeps[] = {{0, 0}};
    test_reset_events(clocks, 3, sleeps, 1);
    struct sample sample;
    struct run_result run;
    const struct config config = test_configuration(1);
    collect_samples(&config, &sample, &run, &test_ops);
    CHECK(run.status == RUN_COMPLETE && run.completed == 1);
    CHECK(test_sleep_deadlines[0] == 1040);
    CHECK(sample.deadline_ns == 40 && sample.skipped_periods == 3);
    CHECK(sample.observed_ns == 41 && sample.latency_ns == 1);
}

static void test_collect_interrupted(void)
{
    const struct test_clock_event clock_events[] = {
        {0, 1000}, {0, 1000}, {0, 1011}, {0, 1011},
    };
    const struct test_sleep_event sleep_events[] = {{0, 0}, {EINTR, SIGTERM}};
    test_reset_events(clock_events, 4, sleep_events, 2);
    struct sample samples[2] = {{0}};
    struct run_result run;
    const struct config config = test_configuration(2);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_INTERRUPTED && run.completed == 1);
    CHECK(run.signal_number == SIGTERM);
    CHECK(test_clock_index == 4 && test_sleep_index == 2);
    CHECK(samples[0].deadline_ns == 10 && samples[0].latency_ns == 1);

    test_reset_events(NULL, 0, NULL, 0);
    stop_signal = SIGINT;
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_INTERRUPTED && run.completed == 0);
    CHECK(run.signal_number == SIGINT);
    CHECK(test_clock_index == 0 && test_sleep_index == 0);
    stop_signal = 0;
}

static void test_collect_failures(void)
{
    struct sample samples[2] = {{0}};
    struct run_result run;
    const struct config config = test_configuration(2);

    const struct test_clock_event initial_failure[] = {{EIO, 0}};
    test_reset_events(initial_failure, 1, NULL, 0);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 0);
    CHECK(run.error_code == EIO && run.error_operation != NULL);

    const struct test_clock_event before_failure[] = {{0, 1000}, {EIO, 0}};
    test_reset_events(before_failure, 2, NULL, 0);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 0 && run.error_code == EIO);
    CHECK(test_sleep_index == 0);

    const struct test_clock_event sleep_clock[] = {{0, 1000}, {0, 1000}};
    const struct test_sleep_event failed_sleep[] = {{ENOTSUP, 0}};
    test_reset_events(sleep_clock, 2, failed_sleep, 1);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 0);
    CHECK(run.error_code == ENOTSUP && test_clock_index == 2);

    const struct test_clock_event after_failure[] = {
        {0, 1000}, {0, 1000}, {0, 1010}, {0, 1011}, {EIO, 0},
    };
    const struct test_sleep_event successes[] = {{0, 0}, {0, 0}};
    test_reset_events(after_failure, 5, successes, 2);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 1 && run.error_code == EIO);
    CHECK(samples[0].deadline_ns == 10 && samples[0].latency_ns == 0);

    const struct test_clock_event backwards_before[] = {{0, 1000}, {0, 999}};
    test_reset_events(backwards_before, 2, NULL, 0);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 0);

    const struct test_clock_event backwards_after[] = {
        {0, 1000}, {0, 1005}, {0, 1004},
    };
    test_reset_events(backwards_after, 3, successes, 1);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 0);

    const struct test_clock_event early_return[] = {
        {0, 1000}, {0, 1000}, {0, 1009},
    };
    test_reset_events(early_return, 3, successes, 1);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 0);

    const struct test_clock_event initial_overflow[] = {{0, INT64_MAX - 9}};
    test_reset_events(initial_overflow, 1, NULL, 0);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 0 && run.error_code == EOVERFLOW);

    const struct test_clock_event next_overflow[] = {
        {0, INT64_MAX - 15}, {0, INT64_MAX - 15}, {0, INT64_MAX - 5},
    };
    test_reset_events(next_overflow, 3, successes, 1);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 1 && run.error_code == EOVERFLOW);
    CHECK(samples[0].deadline_ns == 10 && samples[0].observed_ns == 10);

    const struct config final_config = test_configuration(1);
    test_reset_events(next_overflow, 3, successes, 1);
    collect_samples(&final_config, samples, &run, &test_ops);
    CHECK(run.status == RUN_COMPLETE && run.completed == 1);

    const struct test_clock_event skip_overflow[] = {
        {0, INT64_MAX - 30}, {0, INT64_MAX - 30}, {0, INT64_MAX - 19},
        {0, INT64_MAX},
    };
    test_reset_events(skip_overflow, 4, successes, 1);
    collect_samples(&config, samples, &run, &test_ops);
    CHECK(run.status == RUN_ERROR && run.completed == 1 &&
          run.error_code == EOVERFLOW);
    CHECK(test_sleep_index == 1);
    CHECK(samples[0].deadline_ns == 10 && samples[0].latency_ns == 1);
}

static void test_csv_contents(void)
{
    const struct config config = test_configuration(2);
    const struct sample samples[] = {
        {.deadline_ns = 10, .observed_ns = 13, .latency_ns = 3, .skipped_periods = 0},
        {.deadline_ns = 40, .observed_ns = 40, .latency_ns = 0, .skipped_periods = 2},
    };
    const struct run_result run = {.completed = 2, .status = RUN_COMPLETE};
    FILE *stream = tmpfile();
    CHECK(stream != NULL);
    CHECK(write_csv(stream, &config, samples, &run) == 0);
    CHECK(fflush(stream) == 0);
    CHECK(fseek(stream, 0, SEEK_SET) == 0);
    char text[512];
    const size_t count = fread(text, 1, sizeof(text) - 1, stream);
    CHECK(!ferror(stream));
    text[count] = '\0';
    const char *expected =
        "schema_version,run_status,requested_samples,sample,interval_ns,"
        "deadline_ns,observed_ns,latency_ns,skipped_periods\n"
        "1,complete,2,1,10,10,13,3,0\n"
        "1,complete,2,2,10,40,40,0,2\n";
    CHECK(strcmp(text, expected) == 0);
    CHECK(fclose(stream) == 0);

    const struct run_result interrupted = {
        .completed = 1, .status = RUN_INTERRUPTED, .signal_number = SIGINT,
    };
    stream = tmpfile();
    CHECK(stream != NULL);
    CHECK(write_csv(stream, &config, samples, &interrupted) == 0);
    CHECK(fflush(stream) == 0 && fseek(stream, 0, SEEK_SET) == 0);
    const size_t partial_count = fread(text, 1, sizeof(text) - 1, stream);
    CHECK(!ferror(stream));
    text[partial_count] = '\0';
    CHECK(strstr(text, "1,interrupted,2,1,10,10,13,3,0\n") != NULL);
    CHECK(strstr(text, "1,interrupted,2,2,") == NULL);
    CHECK(fclose(stream) == 0);

    const struct run_result failed = {
        .completed = 1, .status = RUN_ERROR, .error_code = EIO,
        .error_operation = "test clock",
    };
    stream = tmpfile();
    CHECK(stream != NULL);
    CHECK(write_csv(stream, &config, samples, &failed) == 0);
    CHECK(fflush(stream) == 0 && fseek(stream, 0, SEEK_SET) == 0);
    const size_t failed_count = fread(text, 1, sizeof(text) - 1, stream);
    CHECK(!ferror(stream));
    text[failed_count] = '\0';
    CHECK(strstr(text, "1,error,2,1,10,10,13,3,0\n") != NULL);
    CHECK(fclose(stream) == 0);

    const struct run_result empty = {
        .completed = 0, .status = RUN_INTERRUPTED, .signal_number = SIGINT,
    };
    stream = tmpfile();
    CHECK(stream != NULL);
    CHECK(write_csv(stream, &config, NULL, &empty) == 0);
    CHECK(fseek(stream, 0, SEEK_SET) == 0);
    const size_t empty_count = fread(text, 1, sizeof(text) - 1, stream);
    CHECK(!ferror(stream));
    text[empty_count] = '\0';
    CHECK(strcmp(text, "schema_version,run_status,requested_samples,sample,"
                       "interval_ns,deadline_ns,observed_ns,latency_ns,"
                       "skipped_periods\n") == 0);
    CHECK(fclose(stream) == 0);
}

static void test_csv_write_failure(void)
{
    const struct config config = test_configuration(1);
    const struct sample sample = {
        .deadline_ns = 10, .observed_ns = 10, .latency_ns = 0, .skipped_periods = 0,
    };
    const struct run_result run = {.completed = 1, .status = RUN_COMPLETE};
    FILE *stream = fopen("/dev/full", "w");
    CHECK(stream != NULL);
    CHECK(setvbuf(stream, NULL, _IONBF, 0) == 0);
    CHECK(write_csv(stream, &config, &sample, &run) != 0);
    CHECK(ferror(stream));
    (void)fclose(stream);

    stream = fopen("/dev/full", "w");
    CHECK(stream != NULL);
    CHECK(setvbuf(stream, NULL, _IONBF, 0) == 0);
    CHECK(fputs("existing stream error", stream) == EOF);
    CHECK(ferror(stream));
    CHECK(write_csv(stream, &config, &sample, &run) != 0);
    (void)fclose(stream);
}

struct test_output_cookie {
    int write_error;
    int close_error;
    unsigned int write_calls;
    unsigned int close_calls;
};

static ssize_t test_cookie_write(void *opaque, const char *buffer, size_t count)
{
    struct test_output_cookie *cookie = opaque;
    (void)buffer;
    ++cookie->write_calls;
    if (cookie->write_error != 0) {
        errno = cookie->write_error;
        /* GNU cookie write callbacks return zero on error, unlike read. */
        return 0;
    }
    CHECK(count <= (size_t)SSIZE_MAX);
    return (ssize_t)count;
}

static int test_cookie_close(void *opaque)
{
    struct test_output_cookie *cookie = opaque;
    ++cookie->close_calls;
    if (cookie->close_error != 0) {
        errno = cookie->close_error;
        return -1;
    }
    return 0;
}

static FILE *test_cookie_stream(struct test_output_cookie *cookie)
{
    const cookie_io_functions_t functions = {
        .read = NULL,
        .write = test_cookie_write,
        .seek = NULL,
        .close = test_cookie_close,
    };
    FILE *stream = fopencookie(cookie, "w", functions);
    CHECK(stream != NULL);
    return stream;
}

static void test_output_finish_failures(void)
{
    const struct config config = test_configuration(1);
    const struct sample sample = {
        .deadline_ns = 10, .observed_ns = 10, .latency_ns = 0, .skipped_periods = 0,
    };
    const struct run_result run = {.completed = 1, .status = RUN_COMPLETE};
    struct test_output_cookie close_failure = {.close_error = ENOSPC};
    FILE *stream = test_cookie_stream(&close_failure);
    CHECK(finish_output(stream, &config, &sample, &run) == ENOSPC);
    CHECK(close_failure.write_calls > 0 && close_failure.close_calls == 1);

    struct test_output_cookie write_failure = {
        .write_error = ENOSPC, .close_error = EIO,
    };
    stream = test_cookie_stream(&write_failure);
    CHECK(setvbuf(stream, NULL, _IONBF, 0) == 0);
    CHECK(finish_output(stream, &config, &sample, &run) == ENOSPC);
    CHECK(write_failure.write_calls > 0 && write_failure.close_calls == 1);

    struct test_output_cookie flush_failure = {
        .write_error = ENOSPC, .close_error = EIO,
    };
    stream = test_cookie_stream(&flush_failure);
    CHECK(finish_output(stream, &config, &sample, &run) == ENOSPC);
    CHECK(flush_failure.write_calls > 0 && flush_failure.close_calls == 1);

    struct test_output_cookie success = {0};
    stream = test_cookie_stream(&success);
    CHECK(finish_output(stream, &config, &sample, &run) == 0);
    CHECK(success.write_calls > 0 && success.close_calls == 1);
}

int main(void)
{
    test_arithmetic();
    test_positive_parser();
    test_argument_configuration();
    test_skipped_periods();
    test_collect_progression();
    test_collect_eintr();
    test_initial_skips();
    test_collect_interrupted();
    test_collect_failures();
    test_csv_contents();
    test_csv_write_failure();
    test_output_finish_failures();
    printf("C unit tests passed (%u checks).\n", test_assertions);
    return EXIT_SUCCESS;
}
