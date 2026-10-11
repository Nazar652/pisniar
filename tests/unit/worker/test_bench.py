"""The render benchmark: the limit formula, the report lines, the fixtures it builds, its
cleanup, and the boundary that keeps the service layer out of it. The renders themselves
run in the worker container (`make bench-render`).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pisniar.worker import ass, bench, render

BENCH_PATH = Path(bench.__file__)
ALLOWED_PISNIAR_MODULES = {
    "pisniar.worker.ass",
    "pisniar.worker.ffmpeg",
    "pisniar.worker.render",
    "pisniar.worker.turntable",
}


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "pisniar.worker":
                names.update(f"pisniar.worker.{alias.name}" for alias in node.names)
            else:
                names.add(node.module)
    return names


def result(
    *,
    seconds: int = 120,
    count: int = 1,
    wall: float = 100.0,
    python_mb: float = 200.0,
) -> bench.ScenarioResult:
    return bench.ScenarioResult(
        scenario=bench.Scenario(seconds, count),
        wall_seconds=wall,
        cpu_seconds=wall * 1.8,
        python_rss_mb=python_mb,
        ffmpeg_rss_mb=600.0,
        size_bytes=5_500_000,
        steal_percent=1.5,
    )


# ---------------------------------------------------------------- boundary


def test_bench_imports_only_the_render_pipeline() -> None:
    pisniar_imports = {name for name in imported_names(BENCH_PATH) if name.startswith("pisniar")}

    assert pisniar_imports == ALLOWED_PISNIAR_MODULES


@pytest.mark.parametrize("forbidden", ["tasks", "config", "db", "telegram", "bot", "webapp"])
def test_bench_reaches_neither_the_service_layer_nor_the_database(forbidden: str) -> None:
    for name in imported_names(BENCH_PATH):
        assert not name.startswith(f"pisniar.{forbidden}")
        assert name != f"pisniar.worker.{forbidden}"
        assert not name.startswith(("sqlalchemy", "aiogram", "celery", "redis"))


def test_bench_leaves_no_trace_when_a_run_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def failing(root: Path) -> list[str]:
        seen.append(root)
        (root / "left-over.bin").write_bytes(b"x")
        raise RuntimeError("зламалось посеред прогону")

    monkeypatch.setattr(bench, "run_bench", failing)

    with pytest.raises(RuntimeError):
        bench.main()

    assert len(seen) == 1
    assert not seen[0].exists()
    assert seen[0].name.startswith("pisniar-bench-")


def test_main_prints_the_report_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "run_bench", lambda root: ["перший рядок", "другий рядок"])

    assert bench.main() == 0
    assert capsys.readouterr().out == "перший рядок\nдругий рядок\n"


# ---------------------------------------------------------------- the limit formula


def test_ceil_to_step_rounds_up_to_the_next_multiple() -> None:
    assert bench.ceil_to_step(0.5, 30) == 30
    assert bench.ceil_to_step(30, 30) == 30
    assert bench.ceil_to_step(30.1, 30) == 60
    assert bench.ceil_to_step(721, 30) == 750


@pytest.mark.parametrize(
    ("wall", "soft", "hard"),
    [
        (50.0, 600, 660),
        (150.0, 600, 660),
        (195.0, 600, 660),
        (196.0, 630, 690),
        (300.0, 780, 840),
        (400.0, 930, 990),
    ],
)
def test_the_recommendation_is_307_plus_one_and_a_half_walls_with_a_floor(
    wall: float,
    soft: int,
    hard: int,
) -> None:
    recommendation = bench.recommend_limits(wall)

    assert (recommendation.soft_seconds, recommendation.hard_seconds) == (soft, hard)
    assert recommendation.soft_seconds % 30 == 0
    assert recommendation.hard_seconds == recommendation.soft_seconds + 60


def test_the_env_lines_name_the_two_settings() -> None:
    lines = bench.env_lines(bench.LimitRecommendation(soft_seconds=630, hard_seconds=690))

    assert lines == [
        "RENDER_TASK_SOFT_TIME_LIMIT_SECONDS=630",
        "RENDER_TASK_TIME_LIMIT_SECONDS=690",
    ]


# ---------------------------------------------------------------- steal and memory


PROC_STAT = "cpu  100 0 50 800 20 0 5 25 0 0\ncpu0 50 0 25 400 10 0 2 12 0 0\n"


def test_cpu_jiffies_come_from_the_aggregate_line() -> None:
    assert bench.read_cpu_jiffies(PROC_STAT) == (25, 1000)


def test_cpu_jiffies_of_an_old_kernel_without_steal_are_zero_steal() -> None:
    assert bench.read_cpu_jiffies("cpu  100 0 50 800\n") == (0, 950)


def test_a_proc_stat_without_a_cpu_line_is_refused() -> None:
    with pytest.raises(ValueError, match="cpu"):
        bench.read_cpu_jiffies("intr 1 2 3\n")


def test_steal_percent_is_the_share_of_the_interval() -> None:
    assert bench.steal_percent((25, 1000), (35, 1200)) == pytest.approx(5.0)
    assert bench.steal_percent((25, 1000), (25, 1000)) is None


def test_sampling_the_cpu_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Path, *, encoding: str) -> str:
        raise OSError("no /proc here")

    monkeypatch.setattr(Path, "read_text", broken)

    assert bench.sample_cpu_jiffies() is None


def test_megabytes_converts_from_the_kernel_kilobytes() -> None:
    assert bench.megabytes(2048) == 2.0


def test_the_memory_and_ratio_lines() -> None:
    single = result(count=1, wall=100.0, python_mb=200.0)
    double = result(count=2, wall=150.0, python_mb=400.0)

    assert bench.memory_line(single, double) == "пам'ять: ×1 = 800 МБ, ×2 = 1000 МБ із 4096"
    assert bench.ratio_line(single, double) == "×2 / ×1 за часом: 1.50"


def test_a_result_row_shows_every_figure_and_a_missing_steal() -> None:
    row = bench.result_row(result())

    assert "120x1" in row
    assert "wall   100.0 s" in row
    assert "cpu   180.0 s" in row
    assert "size  5.25 MB" in row
    assert "steal 1.5%" in row
    missing = bench.result_row(bench.ScenarioResult(bench.Scenario(45, 1), 1.0, 1.0, 1.0, 1.0, 1, None))
    assert missing.endswith("steal n/a")


def test_the_scenario_label_and_the_three_scenarios() -> None:
    assert [scenario.label for scenario in bench.SCENARIOS] == ["45x1", "120x1", "120x2"]


def test_the_header_has_the_versions_and_the_layer_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench, "ffmpeg_version", lambda: "ffmpeg version 7.1.5-test")

    lines = bench.header_lines(6.5)

    assert lines[0].startswith(f"renderer {render.RENDERER_VERSION}, pillow ")
    assert lines[1] == "ffmpeg version 7.1.5-test"
    assert lines[2] == "шари малюються 6.5 с"


def test_the_summary_has_the_memory_ratio_and_env_lines_and_no_table_rows() -> None:
    results = [
        result(seconds=45, wall=40.0),
        result(seconds=120, wall=100.0),
        result(seconds=120, count=2, wall=150.0, python_mb=400.0),
    ]

    lines = bench.summary_lines(results)

    assert "RENDER_TASK_SOFT_TIME_LIMIT_SECONDS=600" in lines
    assert "RENDER_TASK_TIME_LIMIT_SECONDS=660" in lines
    assert any(line.startswith("пам'ять:") for line in lines)
    assert any(line.startswith("×2 / ×1") for line in lines)
    assert not any("wall" in line for line in lines)


def stub_ffmpeg_version(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    returncode: int = 0,
    fail: bool = False,
) -> list[tuple[str, list[str], float]]:
    calls: list[tuple[str, list[str], float]] = []

    def fake_run(
        binary: str,
        arguments: list[str],
        *,
        timeout_seconds: float,
    ) -> Any:
        calls.append((binary, arguments, timeout_seconds))
        if fail:
            raise bench.ffmpeg.MediaToolNotFoundError(binary)
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    monkeypatch.setattr(bench.ffmpeg, "run_media_tool", fake_run)
    return calls


def test_the_ffmpeg_version_is_the_first_line_read_once_with_a_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub_ffmpeg_version(monkeypatch, stdout="ffmpeg version 7.1.5 Copyright (c) 2000\nbuilt with gcc\n")

    assert bench.ffmpeg_version() == "ffmpeg version 7.1.5 Copyright (c) 2000"
    assert calls == [("ffmpeg", ["-version"], bench.VERSION_TIMEOUT_SECONDS)]
    assert bench.VERSION_TIMEOUT_SECONDS <= 30


@pytest.mark.parametrize(
    "outcome",
    [{"fail": True}, {"stdout": "", "returncode": 0}, {"stdout": "x\n", "returncode": 1}],
)
def test_an_unreadable_ffmpeg_version_is_unknown(monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any]) -> None:
    stub_ffmpeg_version(monkeypatch, **outcome)

    assert bench.ffmpeg_version() == "unknown"


def test_package_version_of_a_missing_package_is_unknown() -> None:
    assert bench.package_version("no-such-package-anywhere") == "unknown"
    assert bench.package_version("pillow") != "unknown"


# ---------------------------------------------------------------- the fixtures


def test_synthetic_lines_are_timed_timed_and_ordered() -> None:
    lines = bench.synthetic_lines(5)

    assert [line.line_index for line in lines] == [0, 1, 2, 3, 4]
    assert [line.start_ms for line in lines] == [1_000, 4_000, 7_000, 10_000, 13_000]
    assert all(line.text == bench.SYNTHETIC_LINE for line in lines)


@pytest.mark.parametrize("length_ms", [45_000, 120_000])
def test_the_synthetic_subtitles_fill_the_fragment_and_overflow_the_display(
    length_ms: int,
) -> None:
    lyrics, display = bench.synthetic_subtitles(length_ms)

    assert lyrics.count("Dialogue: ") >= 3 * (length_ms // 3_000 - 2)
    assert display.count("Dialogue: ") == 4
    assert ass.ELLIPSIS in display


def test_the_synthetic_inputs_are_made_by_ffmpeg_into_the_given_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        binary: str,
        arguments: list[str],
        **kwargs: Any,
    ) -> None:
        calls.append([binary, *arguments])

    monkeypatch.setattr(bench.ffmpeg, "run_media_tool_checked", fake_run)

    inputs = bench.synthesize_inputs(tmp_path)

    assert inputs == bench.BenchInputs(audio=tmp_path / "audio.wav", cover=tmp_path / "cover.jpg")
    assert len(calls) == 2
    assert any(f"anoisesrc=d={bench.SYNTHETIC_AUDIO_SECONDS}" in argument for argument in calls[0])
    assert "yuvj420p" in calls[1]
    assert f"testsrc2=s={bench.COVER_EDGE_PX}x{bench.COVER_EDGE_PX}:d=1" in calls[1]


def test_a_job_gets_its_own_directory_with_a_cover_copy_and_both_subtitle_files(
    tmp_path: Path,
) -> None:
    cover = tmp_path / "source.jpg"
    cover.write_bytes(b"jpeg")
    inputs = bench.BenchInputs(audio=tmp_path / "audio.wav", cover=cover)

    job = bench.prepare_job(inputs, tmp_path / "job-0", length_ms=45_000, lyrics="песня", display="дисплей")

    assert job.cover == tmp_path / "job-0" / render.COVER_FILE_NAME
    assert job.cover.read_bytes() == b"jpeg"
    assert job.subtitles.read_text(encoding="utf-8") == "песня"
    assert job.display_subtitles.read_text(encoding="utf-8") == "дисплей"
    assert job.output.parent == job.cover.parent
    assert job.length_ms == 45_000


# ---------------------------------------------------------------- one render and a scenario


def make_job(tmp_path: Path) -> bench.BenchJob:
    return bench.BenchJob(
        audio=tmp_path / "audio.wav",
        cover=tmp_path / "cover.img",
        display_subtitles=tmp_path / "display.ass",
        subtitles=tmp_path / "lyrics.ass",
        output=tmp_path / "out.mp4.partial",
        length_ms=45_000,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="`resource` and its ru_maxrss are Linux")
def test_a_job_renders_through_the_size_limited_entry_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[render.RenderParameters] = []

    class Probe:
        size_bytes = 4_400_000

    def fake_render(
        parameters: render.RenderParameters,
        output: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[Probe, object]:
        seen.append(parameters)
        return Probe(), render.DEFAULT_QUALITY

    monkeypatch.setattr(bench.render, "render_fragment_within_size_limit", fake_render)

    outcome = bench.run_job(make_job(tmp_path))

    assert seen[0].fragment_start_ms == 0
    assert seen[0].fragment_end_ms == 45_000
    assert seen[0].display_subtitles_path == tmp_path / "display.ass"
    assert seen[0].subtitles_path == tmp_path / "lyrics.ass"
    assert outcome.size_bytes == 4_400_000
    assert outcome.wall_seconds >= 0
    assert outcome.python_rss_mb > 0


def test_a_scenario_sums_its_jobs_and_keeps_the_wall_of_the_whole_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InlinePool:
        def __init__(self, **kwargs: Any) -> None:
            self.max_workers = kwargs["max_workers"]

        def __enter__(self) -> InlinePool:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def map(self, function: Any, jobs: Any) -> Any:
            return map(function, jobs)

    monkeypatch.setattr(bench, "ProcessPoolExecutor", InlinePool)
    monkeypatch.setattr(
        bench,
        "run_job",
        lambda job: bench.JobResult(
            wall_seconds=10.0,
            cpu_seconds=15.0,
            python_rss_mb=100.0,
            ffmpeg_rss_mb=300.0,
            size_bytes=job.length_ms,
        ),
    )
    jobs = [make_job(tmp_path), make_job(tmp_path)]

    combined = bench.run_scenario(bench.Scenario(45, 2), jobs)

    assert combined.cpu_seconds == 30.0
    assert combined.python_rss_mb == 200.0
    assert combined.ffmpeg_rss_mb == 600.0
    assert combined.size_bytes == 45_000
    assert combined.wall_seconds >= 0


def test_measure_layers_times_one_drawing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    drawn: list[Path] = []
    monkeypatch.setattr(bench.turntable, "write_layers", lambda directory: drawn.append(directory))

    assert bench.measure_layers(tmp_path) >= 0
    assert drawn == [tmp_path]


def test_run_bench_prints_the_header_and_each_row_once_and_returns_the_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        bench,
        "synthesize_inputs",
        lambda directory: bench.BenchInputs(directory / "audio.wav", directory / "cover.jpg"),
    )
    (tmp_path / "cover.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr(bench, "measure_layers", lambda directory: 6.0)
    monkeypatch.setattr(bench, "synthetic_subtitles", lambda length_ms: ("песня", "дисплей"))
    scenarios: list[tuple[str, int]] = []

    def fake_scenario(scenario: bench.Scenario, jobs: list[bench.BenchJob]) -> bench.ScenarioResult:
        scenarios.append((scenario.label, len(jobs)))
        return result(seconds=scenario.seconds, count=scenario.count)

    monkeypatch.setattr(bench, "run_scenario", fake_scenario)

    monkeypatch.setattr(bench, "ffmpeg_version", lambda: "ffmpeg version test")

    lines = bench.run_bench(tmp_path)

    assert scenarios == [("45x1", 1), ("120x1", 1), ("120x2", 2)]
    assert any(line.startswith("RENDER_TASK_SOFT_TIME_LIMIT_SECONDS=") for line in lines)
    printed = capsys.readouterr().out.splitlines()
    assert printed[:3] == bench.header_lines(6.0)
    assert [line for line in printed if "wall" in line] == [
        bench.result_row(result(seconds=seconds, count=count)) for seconds, count in ((45, 1), (120, 1), (120, 2))
    ]
