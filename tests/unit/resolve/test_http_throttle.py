"""The throttle and the LRCLIB backoff on a fake clock: the contract the shared fixture zeroes in every other test."""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from types import SimpleNamespace

import httpx
import pytest

import pisniar.resolve.sources
from pisniar.resolve import http
from pisniar.resolve.http import Throttler
from pisniar.resolve.sources import lrclib


def test_every_source_throttle_is_zeroed_for_the_suite_and_none_hides_under_another_name() -> None:
    found: dict[str, Throttler] = {}
    for module_info in pkgutil.iter_modules(pisniar.resolve.sources.__path__):
        module = importlib.import_module(f"pisniar.resolve.sources.{module_info.name}")
        found.update(
            {
                f"{module_info.name}.{name}": value
                for name, value in vars(module).items()
                if isinstance(value, Throttler)
            }
        )

    assert "lrclib.THROTTLE" in found
    assert {key.split(".")[1] for key in found} == {"THROTTLE"}
    assert {key: item.min_interval_seconds for key, item in found.items()} == dict.fromkeys(found, 0.0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=fake.monotonic))
    monkeypatch.setattr(http, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=fake.sleep))
    monkeypatch.setattr(lrclib, "asyncio", SimpleNamespace(sleep=fake.sleep))
    monkeypatch.setattr(lrclib.THROTTLE, "last_call_at", None)
    return fake


async def test_the_first_call_is_free_and_a_quick_second_one_waits_out_the_interval(clock: FakeClock) -> None:
    throttler = Throttler(min_interval_seconds=1.0)

    await throttler.wait()
    clock.now += 0.25
    await throttler.wait()

    assert clock.sleeps == [0.75]


async def test_a_call_after_the_interval_does_not_wait(clock: FakeClock) -> None:
    throttler = Throttler(min_interval_seconds=1.0)

    await throttler.wait()
    clock.now += 1.5
    await throttler.wait()

    assert clock.sleeps == []


async def test_lrclib_backs_off_one_then_two_seconds_before_giving_up(
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lrclib, "RETRY_BASE_DELAY_SECONDS", 1.0)
    transport = httpx.MockTransport(lambda request: httpx.Response(503))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(lrclib.LrclibApiError):
            await lrclib.send_request(client, "https://lrclib.net/api/get", {})

    assert clock.sleeps == [1.0, 2.0]


async def test_lrclib_sleeps_once_when_the_second_attempt_answers(
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lrclib, "RETRY_BASE_DELAY_SECONDS", 1.0)
    answers = iter([503, 200])
    transport = httpx.MockTransport(lambda request: httpx.Response(next(answers)))

    async with httpx.AsyncClient(transport=transport) as client:
        response = await lrclib.send_request(client, "https://lrclib.net/api/get", {})

    assert response.status_code == 200
    assert clock.sleeps == [1.0]
