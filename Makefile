CC ?= cc
PYTHON ?= python3
BUILD_DIR ?= build
WARNINGS := -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror
CFLAGS ?= -std=c11 -O2 $(WARNINGS)
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?=

BINARY := $(BUILD_DIR)/wakeup-latency
UNIT_TEST := $(BUILD_DIR)/test-latency
SANITIZERS := -fsanitize=address,undefined -fno-sanitize-recover=all

.PHONY: all test test-analysis test-pipeline test-plots sanitize clean

all: $(BINARY)

$(BUILD_DIR):
	mkdir -p $@

$(BINARY): src/latency_test.c | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< $(LDFLAGS) $(LDLIBS) -o $@

$(UNIT_TEST): tests/test_latency.c src/latency_test.c | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $< $(LDFLAGS) $(LDLIBS) -o $@

test: $(BINARY) $(UNIT_TEST) test-analysis test-pipeline test-plots
	$(UNIT_TEST)
	$(PYTHON) tests/test_integration.py --binary $(abspath $(BINARY))

test-analysis:
	$(PYTHON) -m unittest discover -s tests -p 'test_analysis.py' -v

test-pipeline: $(BINARY)
	$(PYTHON) tests/test_pipeline.py --binary $(abspath $(BINARY))

test-plots:
	MPLBACKEND=Agg $(PYTHON) -m unittest discover -s tests -p 'test_plotting.py' -v

sanitize:
	$(MAKE) BUILD_DIR=build/sanitize \
		CFLAGS='-std=c11 -O1 -g3 $(WARNINGS) -fno-omit-frame-pointer $(SANITIZERS)' \
		LDFLAGS='$(SANITIZERS)' test

clean:
	rm -rf build
