"""Service-by-service self-test reporting (Phase 28).

The one property that matters: **it must never report a pass it did not observe.** A
self-test that can only say "passed" is decoration; the whole point is to be believed when it
says something is broken. So a collection error, a timeout, or a crashed runner all have to
surface as ❌ with a reason -- never as silence, and never as green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.selftest import SERVICES, ServiceResult, _parse, run_all, run_service


# ================================================================= parsing pytest output
def test_parses_a_clean_pass():
    passed, failed, names = _parse("........   [100%]\n24 passed in 3.41s\n")
    assert (passed, failed, names) == (24, 0, [])


def test_parses_failures_and_names_them():
    out = ("FAILED tests/test_cv_tailor.py::test_master_untouched - AssertionError\n"
           "2 failed, 11 passed in 4.2s\n")
    passed, failed, names = _parse(out)
    assert passed == 11 and failed == 2
    assert any("test_master_untouched" in n for n in names)


def test_parses_collection_errors():
    out = "ERROR tests/test_queue.py - ImportError: no module named x\n1 error in 0.4s\n"
    _, failed, names = _parse(out)
    assert failed == 1 and names


# ================================================================= the message Calvin sees
def test_a_passing_service_reads_like_he_asked():
    """'job hunter passed' — his words."""
    line = ServiceResult(service="job hunter", passed=24, seconds=3.0).line()
    assert line.startswith("✅") and "job hunter passed" in line


def test_a_failing_service_says_what_broke():
    line = ServiceResult(service="CV tailor", passed=10, failed=2,
                         failures=["tests/test_cv_tailor.py::test_master_untouched"]).line()
    assert line.startswith("❌") and "FAILED" in line
    assert "test_master_untouched" in line


def test_a_service_that_could_not_run_is_not_reported_as_passing():
    """The dangerous failure mode: a broken runner looking green."""
    r = ServiceResult(service="music", error="timed out after 600s")
    assert r.ok is False
    assert r.line().startswith("❌") and "timed out" in r.line()


# ================================================================= running for real
def test_run_service_reports_a_genuinely_passing_module(tmp_path):
    """Runs pytest for real against a tiny throwaway test."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n",
                                                   encoding="utf-8")
    res = run_service("demo", ["test_ok.py"], root=tmp_path, timeout=120)
    assert res.ok and res.passed == 1


def test_run_service_reports_a_genuinely_failing_module(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text(
        "def test_bad():\n    assert False, 'boom'\n", encoding="utf-8")
    res = run_service("demo", ["test_bad.py"], root=tmp_path, timeout=120)
    assert res.ok is False and res.failed == 1
    assert any("test_bad" in n for n in res.failures)


def test_an_import_error_is_a_failure_not_a_pass(tmp_path):
    """A module that cannot even be collected must never read as green."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_broken.py").write_text(
        "import a_module_that_does_not_exist\n\ndef test_x():\n    assert True\n",
        encoding="utf-8")
    res = run_service("demo", ["test_broken.py"], root=tmp_path, timeout=120)
    assert res.ok is False


def test_a_missing_module_is_reported_not_skipped_silently(tmp_path):
    (tmp_path / "tests").mkdir()
    res = run_service("ghost", ["test_nope.py"], root=tmp_path)
    assert res.ok is False and "no test modules" in res.error


# ================================================================= streaming to Telegram
def test_each_service_is_reported_as_it_finishes(tmp_path):
    """Results stream, so a long run gives progress instead of silence then a dump."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", "utf-8")
    (tmp_path / "tests" / "test_b.py").write_text("def test_b():\n    assert True\n", "utf-8")
    sent: list[str] = []
    results = run_all(notify=lambda t: sent.append(t) or True,
                      services={"alpha": ["test_a.py"], "beta": ["test_b.py"]},
                      root=tmp_path, timeout=120)
    assert len(results) == 2 and all(r.ok for r in results)
    # start + one per service + summary
    assert len(sent) == 4
    assert "alpha passed" in sent[1] and "beta passed" in sent[2]
    assert sent[-1].startswith("✅") and "2/2 services" in sent[-1]


def test_the_summary_names_what_broke(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", "utf-8")
    (tmp_path / "tests" / "test_b.py").write_text("def test_b():\n    assert False\n", "utf-8")
    sent: list[str] = []
    run_all(notify=lambda t: sent.append(t) or True,
            services={"good": ["test_a.py"], "bad": ["test_b.py"]},
            root=tmp_path, timeout=120)
    assert sent[-1].startswith("❌") and "bad" in sent[-1]


# ================================================================= the map stays honest
def test_every_listed_test_module_exists():
    """A typo here silently drops a whole service from the report — green by omission."""
    tests_dir = Path(__file__).resolve().parent
    missing = [m for mods in SERVICES.values() for m in mods
               if not (tests_dir / m).exists()]
    assert not missing, f"SERVICES references missing modules: {missing}"


def test_every_test_module_is_covered_by_some_service():
    """A new test file that no service claims would never be run by the self-test."""
    tests_dir = Path(__file__).resolve().parent
    claimed = {m for mods in SERVICES.values() for m in mods}
    actual = {p.name for p in tests_dir.glob("test_*.py")}
    # this module itself is the reporter; it does not need to report on itself
    unclaimed = actual - claimed - {"test_selftest.py"}
    assert not unclaimed, f"no service covers: {sorted(unclaimed)}"


def test_a_pass_must_carry_a_real_test_count(tmp_path):
    """"✅ passed (0 tests)" is a hollow green — it means the counts were not parsed.

    The first version passed `-q` on top of pytest.ini's `addopts = -q`, making it `-qq`,
    which suppresses the summary line the counts come from. Every service reported a
    confident pass with zero evidence behind it.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_three.py").write_text(
        "def test_a():\n    assert True\n"
        "def test_b():\n    assert True\n"
        "def test_c():\n    assert True\n", encoding="utf-8")
    # reproduce the real repo's config, which is what caused the bug
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")

    res = run_service("demo", ["test_three.py"], root=tmp_path, timeout=120)
    assert res.ok, res.error
    assert res.passed == 3, f"counts not parsed (got {res.passed}) — the report would be hollow"
    assert "3 tests" in res.line()
