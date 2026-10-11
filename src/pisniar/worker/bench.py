"""`make bench-render`: renders synthetic fragments the way the worker does and prints what a
render costs on this machine, with the `.env` lines for the render time limits. It imports
only the render pipeline: no tasks, no config, no database, no Telegram. Inputs live in a
temporary directory under `/tmp`, never on the media volume, and are removed in a `finally`.
"""

from __future__ import annotations

import math
import multiprocessing
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from pisniar.worker import ass, ffmpeg, render, turntable

SYNTHETIC_AUDIO_SECONDS = 130
COVER_EDGE_PX = 640
SYNTHETIC_LINE_COUNT = 40
LINE_STEP_MS = 3_000
BENCH_TIMEOUT_SECONDS = 3_600.0
VERSION_TIMEOUT_SECONDS = 10.0
KILOBYTES = 1024

# LRCLIB 187 + cover 30 + thumbnail 30 + preview 60: what a render task spends outside ffmpeg.
BUDGET_BASE_SECONDS = 307
# the shared vCPU of a small server is slower and noisier than a laptop.
SAFETY_FACTOR = 1.5
LIMIT_STEP_SECONDS = 30
SOFT_LIMIT_FLOOR_SECONDS = 600
HARD_LIMIT_MARGIN_SECONDS = 60
SERVER_MEMORY_MB = 4096

SYNTHETIC_TITLE = "Дуже довга назва пісні, яка точно не вміститься на дисплеї програвача"
SYNTHETIC_ARTISTS = ("Burned Time Machine", "Другий виконавець із довгим іменем")
SYNTHETIC_LINE = (
    "Цей рядок навмисно довгий, бо гейт показав, що стовідсотково "
    "не влізе в один рядок і його доведеться таки розбивати надвоє."
)


@dataclass(frozen=True)
class Scenario:
    seconds: int
    count: int

    @property
    def label(self) -> str:
        return f"{self.seconds}x{self.count}"


SCENARIOS = (Scenario(45, 1), Scenario(120, 1), Scenario(120, 2))


@dataclass(frozen=True)
class BenchInputs:
    audio: Path
    cover: Path


@dataclass(frozen=True)
class BenchJob:
    audio: Path
    cover: Path
    display_subtitles: Path
    subtitles: Path
    output: Path
    length_ms: int


@dataclass(frozen=True)
class JobResult:
    wall_seconds: float
    cpu_seconds: float
    python_rss_mb: float
    ffmpeg_rss_mb: float
    size_bytes: int


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    wall_seconds: float
    cpu_seconds: float
    python_rss_mb: float
    ffmpeg_rss_mb: float
    size_bytes: int
    steal_percent: float | None


@dataclass(frozen=True)
class LimitRecommendation:
    soft_seconds: int
    hard_seconds: int


def ceil_to_step(value: float, step: int) -> int:
    return math.ceil(value / step) * step


def recommend_limits(wall_seconds_120: float) -> LimitRecommendation:
    """S = 307 + 1.5 x the 120 s wall, rounded up to 30 s and never below the 600/660 floor."""
    wanted = ceil_to_step(BUDGET_BASE_SECONDS + SAFETY_FACTOR * wall_seconds_120, LIMIT_STEP_SECONDS)
    soft = max(wanted, SOFT_LIMIT_FLOOR_SECONDS)
    return LimitRecommendation(soft_seconds=soft, hard_seconds=soft + HARD_LIMIT_MARGIN_SECONDS)


def env_lines(recommendation: LimitRecommendation) -> list[str]:
    return [
        f"RENDER_TASK_SOFT_TIME_LIMIT_SECONDS={recommendation.soft_seconds}",
        f"RENDER_TASK_TIME_LIMIT_SECONDS={recommendation.hard_seconds}",
    ]


def read_cpu_jiffies(text: str) -> tuple[int, int]:
    """(steal, total) from the aggregate `cpu` line of `/proc/stat`."""
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            values = [int(value) for value in fields[1:]]
            steal = values[7] if len(values) > 7 else 0
            return steal, sum(values[:8])
    raise ValueError("у /proc/stat немає рядка cpu")


def steal_percent(before: tuple[int, int], after: tuple[int, int]) -> float | None:
    total = after[1] - before[1]
    if total <= 0:
        return None
    return 100.0 * (after[0] - before[0]) / total


def sample_cpu_jiffies() -> tuple[int, int] | None:
    try:
        return read_cpu_jiffies(Path("/proc/stat").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def synthetic_lines(count: int) -> list[ass.LyricLine]:
    return [
        ass.LyricLine(line_index=index, start_ms=1_000 + index * LINE_STEP_MS, text=SYNTHETIC_LINE)
        for index in range(count)
    ]


def synthetic_subtitles(length_ms: int) -> tuple[str, str]:
    """(song text, display) for a fragment of `length_ms`; the title overflows the display."""
    lyrics = ass.build_ass_subtitles(
        synthetic_lines(SYNTHETIC_LINE_COUNT), fragment_start_ms=0, fragment_end_ms=length_ms
    )
    if lyrics is None:
        raise RuntimeError("синтетичний текст не дав жодної події")
    display = ass.build_display_subtitles(SYNTHETIC_TITLE, SYNTHETIC_ARTISTS, length_ms)
    return lyrics, display


def synthesize_inputs(directory: Path) -> BenchInputs:
    """a pink-noise track and a full-range JPEG cover, both made by ffmpeg itself."""
    audio = directory / "audio.wav"
    cover = directory / "cover.jpg"
    quiet = ["-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    noise = f"anoisesrc=d={SYNTHETIC_AUDIO_SECONDS}:c=pink:r=44100"
    ffmpeg.run_media_tool_checked(
        ffmpeg.FFMPEG_BINARY,
        [*quiet, "-f", "lavfi", "-i", noise, "-ac", "2", str(audio)],
        timeout_seconds=BENCH_TIMEOUT_SECONDS,
    )
    pattern = f"testsrc2=s={COVER_EDGE_PX}x{COVER_EDGE_PX}:d=1"
    ffmpeg.run_media_tool_checked(
        ffmpeg.FFMPEG_BINARY,
        [
            *quiet,
            "-f",
            "lavfi",
            "-i",
            pattern,
            "-frames:v",
            "1",
            "-pix_fmt",
            "yuvj420p",
            str(cover),
        ],
        timeout_seconds=BENCH_TIMEOUT_SECONDS,
    )
    return BenchInputs(audio=audio, cover=cover)


def prepare_job(
    inputs: BenchInputs,
    directory: Path,
    *,
    length_ms: int,
    lyrics: str,
    display: str,
) -> BenchJob:
    """its own directory: the layers are drawn beside the cover, one set per render."""
    directory.mkdir(parents=True)
    cover = directory / render.COVER_FILE_NAME
    shutil.copyfile(inputs.cover, cover)
    lyrics_path = directory / "lyrics.ass"
    display_path = directory / "display.ass"
    lyrics_path.write_text(lyrics, encoding="utf-8")
    display_path.write_text(display, encoding="utf-8")
    return BenchJob(
        audio=inputs.audio,
        cover=cover,
        display_subtitles=display_path,
        subtitles=lyrics_path,
        output=directory / "out.mp4.partial",
        length_ms=length_ms,
    )


def megabytes(kilobytes: float) -> float:
    return kilobytes / KILOBYTES


def run_job(job: BenchJob) -> JobResult:
    """one render in its own process, so its peak memory and CPU time are its own.
    `resource` is imported here: it exists only on Linux, and the host tests import this module."""
    import resource

    started = time.perf_counter()
    parameters = render.RenderParameters(
        audio_path=job.audio,
        cover_path=job.cover,
        fragment_start_ms=0,
        fragment_end_ms=job.length_ms,
        display_subtitles_path=job.display_subtitles,
        subtitles_path=job.subtitles,
    )
    probe, _ = render.render_fragment_within_size_limit(parameters, job.output, timeout_seconds=BENCH_TIMEOUT_SECONDS)
    wall = time.perf_counter() - started
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return JobResult(
        wall_seconds=wall,
        cpu_seconds=own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
        python_rss_mb=megabytes(own.ru_maxrss),
        ffmpeg_rss_mb=megabytes(children.ru_maxrss),
        size_bytes=probe.size_bytes,
    )


def run_scenario(scenario: Scenario, jobs: Sequence[BenchJob]) -> ScenarioResult:
    before = sample_cpu_jiffies()
    started = time.perf_counter()
    with ProcessPoolExecutor(
        max_workers=len(jobs),
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=1,
    ) as pool:
        results = list(pool.map(run_job, jobs))
    wall = time.perf_counter() - started
    after = sample_cpu_jiffies()
    return ScenarioResult(
        scenario=scenario,
        wall_seconds=wall,
        cpu_seconds=sum(result.cpu_seconds for result in results),
        python_rss_mb=sum(result.python_rss_mb for result in results),
        ffmpeg_rss_mb=sum(result.ffmpeg_rss_mb for result in results),
        size_bytes=max(result.size_bytes for result in results),
        steal_percent=None if before is None or after is None else steal_percent(before, after),
    )


def memory_line(single: ScenarioResult, double: ScenarioResult) -> str:
    one = single.python_rss_mb + single.ffmpeg_rss_mb
    two = double.python_rss_mb + double.ffmpeg_rss_mb
    return f"пам'ять: ×1 = {one:.0f} МБ, ×2 = {two:.0f} МБ із {SERVER_MEMORY_MB}"


def ratio_line(single: ScenarioResult, double: ScenarioResult) -> str:
    return f"×2 / ×1 за часом: {double.wall_seconds / single.wall_seconds:.2f}"


def result_row(result: ScenarioResult) -> str:
    steal = "n/a" if result.steal_percent is None else f"{result.steal_percent:.1f}%"
    return (
        f"{result.scenario.label:>8}  wall {result.wall_seconds:7.1f} s  "
        f"cpu {result.cpu_seconds:7.1f} s  "
        f"rss python {result.python_rss_mb:6.0f} MB  ffmpeg {result.ffmpeg_rss_mb:6.0f} MB  "
        f"size {result.size_bytes / (KILOBYTES * KILOBYTES):5.2f} MB  steal {steal}"
    )


def package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def ffmpeg_version() -> str:
    """the first line of `ffmpeg -version`, or "unknown" when it cannot be read."""
    try:
        completed = ffmpeg.run_media_tool(ffmpeg.FFMPEG_BINARY, ["-version"], timeout_seconds=VERSION_TIMEOUT_SECONDS)
    except ffmpeg.MediaToolError:
        return "unknown"
    lines = completed.stdout.splitlines() if completed.returncode == 0 else []
    return lines[0].strip() if lines else "unknown"


def header_lines(layers_seconds: float) -> list[str]:
    """the versions and the layer drawing time, printed before the first scenario."""
    return [
        f"renderer {render.RENDERER_VERSION}, pillow {package_version('pillow')}, python {sys.version.split()[0]}",
        ffmpeg_version(),
        f"шари малюються {layers_seconds:.1f} с",
    ]


def summary_lines(results: Sequence[ScenarioResult]) -> list[str]:
    """the memory lines and the `.env` lines from the 120 s x1 scenario, after the table."""
    by_label = {result.scenario.label: result for result in results}
    single, double = by_label["120x1"], by_label["120x2"]
    recommendation = recommend_limits(single.wall_seconds)
    return [
        memory_line(single, double),
        ratio_line(single, double),
        "додайте в .env і перестворіть bot та worker:",
        *env_lines(recommendation),
    ]


def measure_layers(directory: Path) -> float:
    started = time.perf_counter()
    turntable.write_layers(directory)
    return time.perf_counter() - started


def run_bench(root: Path) -> list[str]:
    inputs = synthesize_inputs(root)
    layers_directory = root / "layers"
    layers_directory.mkdir()
    print("\n".join(header_lines(measure_layers(layers_directory))), flush=True)

    texts = {
        seconds: synthetic_subtitles(seconds * 1_000)
        for seconds in sorted({scenario.seconds for scenario in SCENARIOS})
    }
    results = []
    for scenario in SCENARIOS:
        lyrics, display = texts[scenario.seconds]
        jobs = [
            prepare_job(
                inputs,
                root / f"{scenario.label}-{index}",
                length_ms=scenario.seconds * 1_000,
                lyrics=lyrics,
                display=display,
            )
            for index in range(scenario.count)
        ]
        results.append(run_scenario(scenario, jobs))
        print(result_row(results[-1]), flush=True)
    return summary_lines(results)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pisniar-bench-") as directory:
        lines = run_bench(Path(directory))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
