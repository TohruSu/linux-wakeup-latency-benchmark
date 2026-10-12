"""Optional headless visualization of strictly validated schema v1 observations."""

from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from fractions import Fraction
import math
import os
from pathlib import Path
import stat

from .analysis import INT64_MAX, load_run


FILENAMES = ("histogram.png", "ecdf.png", "latency-vs-sample.png")


class PlotError(ValueError):
    """Invalid plot settings, unavailable dependencies, or rendering/output failure."""


@dataclass(frozen=True)
class PlotReport:
    source: str
    classification: str
    observations: int
    shown: int
    below_range: int
    above_range: int
    range_ns: tuple[int, int] | None
    paths: tuple[str, ...]


def _check_options(bins: int, latency_range_ns: tuple[int, int] | None) -> None:
    if type(bins) is not int or not 1 <= bins <= 10000:
        raise PlotError("bins must be an integer between 1 and 10000")
    if latency_range_ns is not None:
        if (not isinstance(latency_range_ns, (tuple, list)) or
                len(latency_range_ns) != 2 or
                any(type(value) is not int for value in latency_range_ns)):
            raise PlotError("latency range requires two integer nanosecond bounds")
        lower, upper = latency_range_ns
        if not 0 <= lower < upper <= INT64_MAX:
            raise PlotError("latency range must satisfy 0 <= MIN < MAX <= INT64_MAX")


def _ecdf(values: list[int]) -> tuple[list[int], list[float]]:
    """Unique integer values and their cumulative probabilities, including ties."""
    if not values:
        raise PlotError("an ECDF requires at least one observation")
    counts = Counter(values)
    ordered = sorted(counts)
    cumulative = 0
    probabilities = []
    for value in ordered:
        cumulative += counts[value]
        probabilities.append(cumulative / len(values))
    return ordered, probabilities


def _histogram(values: list[int], bins: int, lower: int,
               upper: int) -> tuple[list[int], list[float]]:
    # Count using integers so float conversion cannot lose or mis-bin samples.
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, (value - lower) * bins // (upper - lower))
        counts[index] += 1
    edges = [float(Fraction(lower * (bins - index) + upper * index, bins))
             for index in range(bins + 1)]
    if any(right <= left for left, right in zip(edges, edges[1:])):
        raise PlotError("range is too narrow for floating-point plot axes at this "
                        "magnitude; use wider bounds or fewer bins")
    return counts, edges


def _write_figures(figures: list, output_dir: Path, overwrite: bool) -> tuple[str, ...]:
    if (not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW")) or
            os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd):
        raise PlotError("plot file output requires POSIX directory-descriptor support; "
                        "Linux is the initial supported platform")
    output_dir.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = os.open(output_dir, flags)
    try:
        # Check the entire destination set before ordinary writes/truncation.
        for name in FILENAMES:
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not overwrite:
                raise PlotError(f"output already exists: {output_dir / name}; use --overwrite")
            if not stat.S_ISREG(info.st_mode):
                raise PlotError(f"output must be a regular file: {output_dir / name}")
        with ExitStack() as stack:
            streams = []
            for name in FILENAMES:
                file_flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
                if not overwrite:
                    file_flags |= os.O_EXCL
                fd = os.open(name, file_flags, 0o666, dir_fd=directory_fd)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise PlotError(f"output must be a regular file: {output_dir / name}")
                    stream = os.fdopen(fd, "wb")
                except BaseException:
                    os.close(fd)
                    raise
                streams.append(stack.enter_context(stream))
            if overwrite:
                for stream in streams:
                    os.ftruncate(stream.fileno(), 0)
            for figure, stream in zip(figures, streams):
                figure.savefig(stream, format="png", dpi=150)
                stream.flush()
        return tuple(str(output_dir / name) for name in FILENAMES)
    finally:
        os.close(directory_fd)


def plot_run(path: str | os.PathLike[str], output_dir: str | os.PathLike[str], *,
             bins: int = 50, latency_range_ns: tuple[int, int] | None = None,
             overwrite: bool = False) -> PlotReport:
    """Validate first, then render three descriptive PNGs using an Agg canvas.

    Zoom affects distribution axes only. Inclusive range counts remain exact;
    ECDF probabilities use the full observation count and the sample plot keeps
    every row. Output failure can leave partial files; no atomic set is claimed.
    """
    _check_options(bins, latency_range_ns)
    run = load_run(path)
    if not run.samples:
        raise PlotError("header-only run has no observations to plot; metadata is unknown")
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as error:
        raise PlotError("plotting requires optional Matplotlib; install with "
                        "python3 -m pip install -r requirements-plot.txt") from error

    latencies = [row.latency_ns for row in run.samples]
    below = above = 0
    if latency_range_ns is None:
        lower, upper = min(latencies), max(latencies)
        # Keep constant/narrow large-integer data away from degenerate float bins.
        padding = max(1, math.ceil(math.ulp(float(max(abs(lower), abs(upper))))) * (bins + 1))
        if upper - lower < padding:
            lower -= padding
            upper += padding
        visible = latencies
        selected_range = None
    else:
        lower, upper = latency_range_ns
        below = sum(value < lower for value in latencies)
        above = sum(value > upper for value in latencies)
        visible = [value for value in latencies if lower <= value <= upper]
        selected_range = (lower, upper)
    counts, edges = _histogram(visible, bins, lower, upper)
    x_ecdf, y_ecdf = _ecdf(latencies)
    n = len(latencies)
    heading = (f"{Path(run.source).name}\n{run.classification}; "
               f"{n}/{run.requested_samples} observations; declared status: {run.declared_status}")
    accounting = (f"Distribution shown: {len(visible)}/{n}; below range: {below}; "
                  f"above range: {above}.")
    if selected_range is not None:
        accounting += f" Inclusive range: [{lower}, {upper}] ns."
    skipped = sum(row.skipped_periods for row in run.samples)
    footer = (f"{accounting}\nECDF denominator: all {n} observations; sample plot retains all rows. "
              f"Recorded skipped periods: {skipped}.\n"
              "Application-observed wake-up lateness; descriptive, no real-time guarantee.")
    figures = []
    try:
        for title in ("Latency histogram", "Empirical cumulative distribution", "Latency versus sample"):
            figure = Figure(figsize=(9, 5))
            FigureCanvasAgg(figure)
            axis = figure.add_subplot(1, 1, 1)
            figure.suptitle(heading, fontsize=11)
            axis.set_title(title, fontsize=10)
            figure.text(0.04, 0.04, footer, fontsize=8)
            figure.subplots_adjust(bottom=0.27, top=0.76, left=0.12, right=0.96)
            axis.grid(True, alpha=0.25)
            figures.append(figure)
        histogram_axis = figures[0].axes[0]
        histogram_axis.stairs(counts, edges, fill=True, alpha=0.75)
        histogram_axis.set_xlabel("Wake-up lateness (ns)")
        histogram_axis.set_ylabel("Observation count")
        histogram_axis.set_xlim(edges[0], edges[-1])
        ecdf_axis = figures[1].axes[0]
        ecdf_axis.step([min(lower, x_ecdf[0]), *x_ecdf, max(upper, x_ecdf[-1])],
                       [0.0, *y_ecdf, 1.0], where="post")
        ecdf_axis.set_xlabel("Wake-up lateness (ns)")
        ecdf_axis.set_ylabel("Empirical cumulative probability\n(all observations)")
        ecdf_axis.set_xlim(edges[0], edges[-1])
        ecdf_axis.set_ylim(0, 1.02)
        # A jump on a zoom boundary can hide behind the axes spine. Mark its
        # right-continuous value so inclusive endpoints remain visible.
        boundary_probabilities = [sum(value <= bound for value in latencies) / n
                                  for bound in (lower, upper)]
        ecdf_axis.scatter([lower, upper], boundary_probabilities,
                          s=14, zorder=3, clip_on=False)
        sample_axis = figures[2].axes[0]
        sample_axis.plot([row.sample for row in run.samples], latencies,
                         linewidth=0.8, marker=".", markersize=3)
        sample_axis.set_xlabel("Completed sample index")
        sample_axis.set_ylabel("Wake-up lateness (ns)")
        paths = _write_figures(figures, Path(output_dir), overwrite)
    except (OSError, ValueError, RuntimeError) as error:
        raise PlotError(f"plot output failed: {error}; output may be incomplete") from error
    finally:
        for figure in figures:
            figure.clear()
    return PlotReport(run.source, run.classification, n, len(visible), below, above,
                      selected_range, paths)
