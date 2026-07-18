"""Service-by-service self-test with live Telegram reporting (Phase 28).

Calvin: "run tests and return a check on every passed test and x on every failed test on the
telegram ... lets say we are testing the job service ... return job hunter passed".

The value is not that it runs pytest -- he can do that. It is that results arrive on his phone
grouped by SERVICE, as they finish, so a deploy can be verified from anywhere without reading
a wall of dots. A single "540 passed" tells you nothing about which capability broke.

Two design choices worth stating:

* **Reports per service, streamed.** Each group is sent the moment it finishes, so a long run
  gives progress rather than silence then a dump.
* **Never fabricates a pass.** If a group errors, times out, or the runner itself falls over,
  it reports ❌ with the reason. A self-test that can only say "passed" is decoration -- the
  whole point is to be believed when it says something is broken.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.logging_setup import get_logger

log = get_logger("core.selftest")

# Human-facing service name -> the test modules that cover it. Ordered roughly by how much
# Calvin depends on the capability, so the important results land first.
SERVICES: dict[str, list[str]] = {
    "job hunter": ["test_job_hunter.py", "test_job_scoring.py", "test_job_sources.py"],
    "CV tailor": ["test_cv_tailor.py", "test_cv_pdf.py"],
    "email agent": ["test_email_agent.py"],
    "persona": ["test_persona.py", "test_github_import.py"],
    "job queue / workers": ["test_queue.py"],
    "approvals": ["test_approvals.py"],
    "proactive triage": ["test_proactive.py"],
    "semantic memory": ["test_semantic.py"],
    "interview prep": ["test_interview_prep.py", "test_form_assist.py"],
    "study vault": ["test_vault.py", "test_lecture_capture.py", "test_spaced_rep.py"],
    "semester planner": ["test_semester_planner.py"],
    "code tutor": ["test_code_tutor.py"],
    "event scout": ["test_event_scout.py"],
    "deal broker": ["test_deal_broker.py", "test_deal_broker_multi.py", "test_margin_ledger.py"],
    "music": ["test_music.py"],
    "desktop control": ["test_desktop.py", "test_assistant_core.py"],
    "infra recon": ["test_infra_recon.py"],
    "adaptive layer": ["test_adaptive.py"],
    "voice": ["test_voice.py"],
    "telegram bot": ["test_telegram.py"],
    "session continuity": ["test_session.py"],
    "kernel & routing": ["test_kernel.py", "test_intent.py", "test_router.py",
                         "test_llm_routing.py", "test_memory.py"],
}


@dataclass
class ServiceResult:
    service: str
    passed: int = 0
    failed: int = 0
    seconds: float = 0.0
    error: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.error

    def line(self) -> str:
        mark = "✅" if self.ok else "❌"
        if self.error:
            return f"{mark} {self.service} — could not run: {self.error[:90]}"
        if self.ok:
            return f"{mark} {self.service} passed ({self.passed} tests, {self.seconds:.0f}s)"
        return (f"{mark} {self.service} FAILED — {self.failed} of {self.passed + self.failed}"
                + (":\n     " + "\n     ".join(self.failures[:3]) if self.failures else ""))


def _parse(output: str) -> tuple[int, int, list[str]]:
    """Pull pass/fail counts and failing test names out of pytest's summary."""
    passed = failed = 0
    names: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            names.append(line.split(" - ")[0].replace("FAILED ", "").replace("ERROR ", "")[:80])
        # e.g. "12 passed, 2 failed in 3.4s" -- a count only counts when a NUMBER precedes the
        # word. `last` starts as None because pytest also prints bare prose containing these
        # words ("ERROR: file or directory not found: tests/x.py"), and reading a number that
        # was never there crashed the whole run with UnboundLocalError.
        if " passed" in line or " failed" in line or " error" in line:
            last: str | None = None
            for chunk in line.replace(",", " ").split():
                if chunk.isdigit():
                    last = chunk
                elif last is not None and chunk.startswith("passed"):
                    passed = max(passed, int(last))
                elif last is not None and chunk.startswith(("failed", "error")):
                    failed = max(failed, int(last))
    return passed, failed, names


def run_service(service: str, modules: list[str], *, root: Path,
                timeout: int = 600) -> ServiceResult:
    """Run one service's tests in a subprocess so a crash can't take the reporter down."""
    present = [m for m in modules if (root / "tests" / m).exists()]
    if not present:
        return ServiceResult(service=service, error="no test modules found")
    started = time.time()
    try:
        # No -q here: pytest.ini already sets `addopts = -q`, so passing it again makes it
        # -qq, which suppresses the summary line ("24 passed in 3.4s") entirely -- the very
        # line these counts are parsed from. The first run of this reported a confident
        # "✅ passed (0 tests)" for every service, which is exactly the kind of hollow green
        # this tool exists to avoid.
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
             *[f"tests/{m}" for m in present]],
            cwd=str(root), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ServiceResult(service=service, seconds=time.time() - started,
                             error=f"timed out after {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return ServiceResult(service=service, error=f"{type(exc).__name__}: {exc}")

    passed, failed, names = _parse(proc.stdout + proc.stderr)
    result = ServiceResult(service=service, passed=passed, failed=failed,
                           seconds=time.time() - started, failures=names)
    # A non-zero exit with no parsed failure means something broke outside the tests
    # (collection error, import error). Report it rather than showing a misleading green.
    if proc.returncode != 0 and failed == 0 and not names:
        result.error = (proc.stdout + proc.stderr).strip().splitlines()[-1][:120] if (
            proc.stdout or proc.stderr) else f"exit code {proc.returncode}"
    return result


def run_all(notify: Callable[[str], bool] | None = None, *,
            services: dict[str, list[str]] | None = None,
            root: Path | None = None, timeout: int = 600) -> list[ServiceResult]:
    """Run every service's tests, reporting each result as it lands."""
    from core.notify import send_telegram

    push = notify or send_telegram
    root = root or Path(__file__).resolve().parents[1]
    groups = services or SERVICES

    push(f"🧪 Self-test starting — {len(groups)} services.")
    results: list[ServiceResult] = []
    for service, modules in groups.items():
        result = run_service(service, modules, root=root, timeout=timeout)
        results.append(result)
        push(result.line())
        log.info("selftest %s: %s", service, "ok" if result.ok else "FAILED")

    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    total_tests = sum(r.passed + r.failed for r in results)
    summary = [f"{'✅' if not bad else '❌'} Self-test complete — "
               f"{len(ok)}/{len(results)} services passed ({total_tests} tests)."]
    if bad:
        summary.append("Broken: " + ", ".join(r.service for r in bad))
    push("\n".join(summary))
    return results
