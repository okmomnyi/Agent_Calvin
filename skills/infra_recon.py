"""Self-audit / infra recon (Phase 21).

A scheduled security pass over Calvin's OWN VPS-hosted projects — his CTF/security interest
pointed at his own footprint.

Two hard rules, both structural:

1. **Report only.** This skill never restarts a service, never patches, never changes
   anything. It flags, ranked by severity. That's the same spirit as §0 Principle 3 — and it
   also means a false positive can never take something live down.
2. **Enrolled targets only.** It scans nothing Calvin hasn't explicitly enrolled. Port-
   scanning someone else's host is not a thing this agent will ever do by accident.

Checks: open ports vs expected, TLS expiry window, config/.env files reachable over HTTP,
dependencies with known CVEs (via OSV.dev — free, no key), and PM2/Docker container health.
A finding that persists across scans ESCALATES rather than repeating identically forever.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.infra_recon")

_TARGETS_KV = "infra.targets"
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
ESCALATE_AFTER = 3          # scans in a row before a finding is called out as recurring

# Paths that must never be readable over HTTP.
SENSITIVE_PATHS = ["/.env", "/.env.example", "/config.yaml", "/.git/config",
                   "/docker-compose.yml", "/token.json", "/credentials.json"]
OSV_URL = "https://api.osv.dev/v1/querybatch"


class InfraReconSkill(BaseSkill):
    name = "infra_recon"

    def __init__(self, memory: Memory | None = None,
                 port_checker: Callable[[str, int], bool] | None = None,
                 tls_checker: Callable[[str], float | None] | None = None,
                 http_get: Callable[[str], int] | None = None,
                 osv_query: Callable[[list[dict[str, Any]]], list[list[str]]] | None = None,
                 runner: Callable[[list[str]], str | None] | None = None,
                 notify: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._port = port_checker or _port_open
        self._tls = tls_checker or _tls_expiry
        self._http = http_get or _http_status
        self._osv = osv_query or _osv_query
        self._run = runner or _run
        self._notify = notify or send_telegram
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"enroll": self.enroll, "targets": self.targets, "scan": self.scan,
                "report": self.report, "findings": self.report}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [ScheduledJob(id="infra.scan", func=self.scan, trigger="cron",
                             kwargs={"day_of_week": "sun", "hour": 6})]

    def contract(self) -> SkillContract:
        """No standing instruction steers a security scan, and it can never act."""
        return SkillContract(reads_categories=[],
                             hard_invariants=["report_only_never_acts", "enrolled_targets_only"])

    # ------------------------------------------------------------- enrolment
    def _targets(self) -> dict[str, list[int]]:
        raw = self.mem.kv_get(_TARGETS_KV)
        if raw:
            return json.loads(raw)
        return dict(get_settings().get("infra", "targets", default={}) or {})

    def enroll(self, target: str = "", ports: str = "", **_: Any) -> CommandResult:
        """Explicitly enroll a host you own. Nothing is ever scanned without this."""
        if not target:
            return CommandResult(text="Usage: enroll <host> [--ports 80,443]", ok=False)
        expected = [int(p) for p in str(ports).replace(",", " ").split() if p.strip().isdigit()]
        targets = self._targets()
        targets[target] = expected or [80, 443]
        self.mem.kv_set(_TARGETS_KV, json.dumps(targets))
        return CommandResult(
            text=f"Enrolled {target} (expected open ports: {targets[target]}). "
                 f"I'll scan it weekly and only ever report.",
            data={"targets": targets})

    def targets(self, **_: Any) -> CommandResult:
        t = self._targets()
        if not t:
            return CommandResult(text="No targets enrolled. `enroll <host>` first — I scan "
                                      "nothing you haven't explicitly enrolled.", data={"targets": {}})
        lines = ["🎯 Enrolled targets (weekly, report-only):"]
        lines += [f"  {host} — expected ports {ports}" for host, ports in t.items()]
        return CommandResult(text="\n".join(lines), data={"targets": t})

    # ------------------------------------------------------------- scan
    def scan(self, notify: bool = True, **_: Any) -> CommandResult:
        """Run every check against enrolled targets. Records findings; changes nothing."""
        targets = self._targets()
        if not targets:
            return CommandResult(text="Nothing enrolled to scan.", data={"findings": 0})
        started = self._now()
        found = 0
        for host, expected in targets.items():
            found += self._check_ports(host, expected)
            found += self._check_tls(host)
            found += self._check_exposed(host)
            self.mem.resolve_unseen(host, started)      # fixed things close themselves
        found += self._check_dependencies()
        found += self._check_containers()
        text = self.report(notify=False).text
        if notify:
            self._notify(text)
        return CommandResult(text=text, data={"findings": found, "targets": list(targets)})

    def _record(self, target: str, check: str, detail: str, severity: str) -> int:
        return self.mem.record_finding(target, check, detail, severity, now=self._now())

    def _check_ports(self, host: str, expected: list[int]) -> int:
        """Anything open that shouldn't be is the finding — expected ports are fine."""
        scan_ports = sorted(set(expected) | set(
            get_settings().get("infra", "probe_ports", default=[22, 80, 443, 3000, 5432, 8000, 8080]) or []))
        n = 0
        for port in scan_ports:
            try:
                is_open = self._port(host, port)
            except Exception:  # noqa: BLE001 - a probe failure is not a finding
                continue
            if is_open and port not in expected:
                sev = "high" if port in (5432, 3000, 8000, 8080) else "medium"
                self._record(host, "open_port", f"port {port} is open but not expected", sev)
                n += 1
        return n

    def _check_tls(self, host: str) -> int:
        try:
            expires = self._tls(host)
        except Exception:  # noqa: BLE001
            return 0
        if expires is None:
            return 0
        days = (expires - self._now()) / 86400
        window = float(get_settings().get("infra", "tls_warn_days", default=21))
        if days < 0:
            self._record(host, "tls_expiry", "TLS certificate has EXPIRED", "critical")
            return 1
        if days <= window:
            self._record(host, "tls_expiry", f"TLS certificate expires in {days:.0f} days",
                         "high" if days <= 7 else "medium")
            return 1
        return 0

    def _check_exposed(self, host: str) -> int:
        """A 200 on any of these means secrets are readable over HTTP — always critical.

        No web port open means nothing can be exposed over one, so don't ask: probing a
        closed port costs a full retry-and-backoff cycle per path for an answer we have.
        """
        scheme = "https" if self._port(host, 443) else "http" if self._port(host, 80) else ""
        if not scheme:
            return 0
        n = 0
        for path in SENSITIVE_PATHS:
            try:
                status = self._http(f"{scheme}://{host}{path}")
            except Exception:  # noqa: BLE001
                continue
            if status == 200:
                self._record(host, "exposed_file", f"{path} is readable over HTTP", "critical")
                n += 1
        return n

    def _check_dependencies(self) -> int:
        """Known CVEs in what we actually have installed, via OSV.dev (free, no key)."""
        pkgs = self._parse_requirements()
        if not pkgs:
            return 0
        try:
            results = self._osv(pkgs)
        except Exception:  # noqa: BLE001
            log.warning("OSV lookup failed — skipping the CVE check this run")
            return 0
        n = 0
        for pkg, vulns in zip(pkgs, results):
            for vuln_id in vulns:
                self._record("local:requirements.txt", "cve",
                             f"{pkg['package']['name']} {pkg['version']}: {vuln_id}", "high")
                n += 1
        return n

    def _parse_requirements(self) -> list[dict[str, Any]]:
        """Which packages to check (manifest) at which versions (**the installed ones**).

        OSV needs an exact version. Taking it from the manifest only works if every line is
        `==`-pinned — ours are ranges, which silently checked nothing at all. The version that
        matters is the one actually installed and running, and `importlib.metadata` knows it
        exactly, so nothing here is guessed. A requirement that isn't installed is skipped
        rather than assumed.
        """
        from importlib.metadata import PackageNotFoundError, version as installed_version

        req = get_settings().project_root / "requirements.txt"
        if not req.exists():
            return []
        out = []
        for line in req.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):        # skip -r/-e/--flag lines
                continue
            name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
            if not name:
                continue
            try:
                ver = installed_version(name)
            except PackageNotFoundError:
                continue                                # not installed -> can't check honestly
            out.append({"package": {"name": name, "ecosystem": "PyPI"}, "version": ver})
        return out

    def _check_containers(self) -> int:
        """PM2 / Docker health — reported, never restarted."""
        n = 0
        pm2 = self._run(["pm2", "jlist"])
        if pm2:
            try:
                for proc in json.loads(pm2):
                    status = proc.get("pm2_env", {}).get("status")
                    restarts = proc.get("pm2_env", {}).get("restart_time", 0)
                    if status != "online":
                        self._record("local:pm2", "container",
                                     f"{proc.get('name')} is {status}", "high")
                        n += 1
                    elif restarts and int(restarts) >= 5:
                        self._record("local:pm2", "container",
                                     f"{proc.get('name')} has restarted {restarts}× (crash loop?)",
                                     "medium")
                        n += 1
            except (ValueError, TypeError):
                log.debug("could not parse pm2 jlist output")
        docker = self._run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
        if docker:
            for line in docker.strip().splitlines():
                name, _, status = line.partition("\t")
                if status and ("unhealthy" in status.lower() or "restarting" in status.lower()):
                    self._record("local:docker", "container", f"{name}: {status}", "high")
                    n += 1
        return n

    # ------------------------------------------------------------- report
    def report(self, notify: bool = True, **_: Any) -> CommandResult:
        """Severity-ranked digest. Recurring findings escalate instead of repeating verbatim."""
        rows = self.mem.open_findings()
        if not rows:
            return CommandResult(text="🛡 Infra scan: nothing open. Clean.", data={"open": 0})
        lines = [f"🛡 Infra findings ({len(rows)} open) — report only, nothing was changed:"]
        recurring = 0
        for r in rows:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡",
                    "low": "🔵"}.get(r["severity"], "⚪")
            line = f"{icon} [{r['severity']}] {r['target']} — {r['detail']}"
            if r["occurrences"] >= ESCALATE_AFTER:
                # don't nag identically forever — escalate it
                line += f"  ⚠️ RECURRING ×{r['occurrences']} — this keeps coming back; fix it or accept it"
                recurring += 1
            lines.append(line)
        text = "\n".join(lines)
        if notify:
            self._notify(text)
        return CommandResult(text=text, data={"open": len(rows), "recurring": recurring,
                                              "findings": [dict(r) for r in rows]})


# ------------------------------------------------------------------ real probes (injectable)
def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _tls_expiry(host: str, port: int = 443, timeout: float = 5.0) -> float | None:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert = ssock.getpeercert()
    not_after = cert.get("notAfter") if cert else None
    if not not_after:
        return None
    dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _http_status(url: str) -> int:
    from skills.job_hunter.fetcher import Fetcher

    resp = Fetcher(respect_robots=False).get(url)   # our own host; we're auditing it
    return resp.status_code if resp is not None else 0


def _osv_query(pkgs: list[dict[str, Any]]) -> list[list[str]]:
    """Batch-query OSV.dev for known vulns. Free and keyless."""
    import requests

    resp = requests.post(OSV_URL, json={"queries": pkgs}, timeout=30)
    resp.raise_for_status()
    return [[v.get("id", "?") for v in (r.get("vulns") or [])]
            for r in resp.json().get("results", [])]


def _run(cmd: list[str]) -> str | None:
    """Run a READ-ONLY inspection command. Never used for mutating operations."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


SKILL = InfraReconSkill()
