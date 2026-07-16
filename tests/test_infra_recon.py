"""Self-audit / infra recon (Phase 21).

Two rules matter most and both are structural:
  * REPORT ONLY — it flags, it never restarts/patches/changes anything;
  * ENROLLED TARGETS ONLY — it never touches a host Calvin didn't explicitly enroll.
Every probe (sockets, TLS, HTTP, OSV, pm2/docker) is injected, so tests are offline.
"""

from __future__ import annotations

import pytest

from skills.infra_recon import ESCALATE_AFTER, InfraReconSkill

NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def recon(mem):
    calls: dict[str, list] = {"ports": [], "tls": [], "http": [], "run": []}

    def port(host, p):
        calls["ports"].append((host, p))
        return p in (80, 443, 5432)          # 5432 is open but NOT expected -> finding

    def tls(host):
        calls["tls"].append(host)
        return NOW + 5 * DAY                  # expires in 5 days -> high

    def http(url):
        calls["http"].append(url)
        return 200 if url.endswith("/.env") else 404   # .env exposed -> critical

    def osv(pkgs):
        return [["GHSA-xxxx-yyyy"] if p["package"]["name"] == "requests" else [] for p in pkgs]

    def run(cmd):
        calls["run"].append(cmd)
        if cmd[:2] == ["pm2", "jlist"]:
            return '[{"name":"agentos-bot","pm2_env":{"status":"errored","restart_time":9}}]'
        if cmd[:2] == ["docker", "ps"]:
            return "agentos-pg\tUp 3 days (unhealthy)\n"
        return None

    skill = InfraReconSkill(memory=mem, port_checker=port, tls_checker=tls, http_get=http,
                            osv_query=osv, runner=run, notify=lambda t: True,
                            clock=lambda: NOW)
    return skill, calls


# ================================================================= enrolment gate
def test_scans_nothing_unless_enrolled(recon):
    skill, calls = recon
    res = skill.scan(notify=False)
    assert res.data["findings"] == 0
    assert calls["ports"] == [] and calls["tls"] == [] and calls["http"] == []


def test_enroll_then_only_that_target_is_touched(recon):
    skill, calls = recon
    skill.enroll(target="agent.example.com", ports="80,443")
    skill.scan(notify=False)
    assert {h for h, _ in calls["ports"]} == {"agent.example.com"}
    assert calls["tls"] == ["agent.example.com"]


def test_targets_view(recon):
    skill, _ = recon
    assert "No targets enrolled" in skill.targets().text
    skill.enroll(target="example.test", ports="443")
    assert "example.test" in skill.targets().text


# ================================================================= checks
def test_unexpected_open_port_is_flagged(mem, recon):
    skill, _ = recon
    skill.enroll(target="h.test", ports="80,443")
    skill.scan(notify=False)
    ports = [f for f in mem.open_findings() if f["check_type"] == "open_port"]
    assert len(ports) == 1
    assert "5432" in ports[0]["detail"] and ports[0]["severity"] == "high"


def test_expected_ports_are_not_findings(mem, recon):
    skill, _ = recon
    skill.enroll(target="h.test", ports="80,443,5432")     # postgres expected here
    skill.scan(notify=False)
    assert [f for f in mem.open_findings() if f["check_type"] == "open_port"] == []


def test_tls_expiry_window(mem, recon):
    skill, _ = recon
    skill.enroll(target="h.test", ports="443")
    skill.scan(notify=False)
    tls = [f for f in mem.open_findings() if f["check_type"] == "tls_expiry"][0]
    assert "5 days" in tls["detail"] and tls["severity"] == "high"


def test_expired_cert_is_critical(mem, recon):
    skill, _ = recon
    skill._tls = lambda host: NOW - DAY          # already expired
    skill.enroll(target="h.test", ports="443")
    skill.scan(notify=False)
    tls = [f for f in mem.open_findings() if f["check_type"] == "tls_expiry"][0]
    assert tls["severity"] == "critical" and "EXPIRED" in tls["detail"]


def test_exposed_env_file_is_critical(mem, recon):
    skill, _ = recon
    skill.enroll(target="h.test", ports="443")
    skill.scan(notify=False)
    exposed = [f for f in mem.open_findings() if f["check_type"] == "exposed_file"]
    assert exposed and exposed[0]["severity"] == "critical" and "/.env" in exposed[0]["detail"]


def test_no_web_port_means_no_http_probing(mem, recon):
    """A host with no 80/443 can't expose a file over HTTP — asking costs retries for nothing."""
    skill, calls = recon
    skill._port = lambda host, p: p == 5432        # database host, no web server
    skill.enroll(target="db.test", ports="5432")
    skill.scan(notify=False)
    assert calls["http"] == []
    assert [f for f in mem.open_findings() if f["check_type"] == "exposed_file"] == []


def test_plain_http_host_is_still_probed(recon):
    skill, calls = recon
    skill._port = lambda host, p: p == 80          # port 80 only, no TLS
    skill.enroll(target="old.test", ports="80")
    skill.scan(notify=False)
    assert all(u.startswith("http://old.test") for u in calls["http"])


@pytest.fixture
def manifest(monkeypatch, tmp_path):
    """Point the recon skill at a throwaway requirements.txt."""
    import skills.infra_recon as ir

    real = ir.get_settings()

    class _S:
        def __init__(self): self.project_root = tmp_path
        def __getattr__(self, n): return getattr(real, n)

    monkeypatch.setattr(ir, "get_settings", lambda: _S())

    def write(text: str):
        (tmp_path / "requirements.txt").write_text(text, encoding="utf-8")
    return write


def test_cve_check_uses_the_installed_version_not_the_manifest_range(mem, recon, manifest):
    """A `>=` range has no version to check — the running one does, and that's what matters."""
    skill, _ = recon
    manifest("requests>=2.31        # a range, like every line we actually ship\n")
    skill._check_dependencies()
    cves = [f for f in mem.open_findings() if f["check_type"] == "cve"]
    assert len(cves) == 1                       # would have been 0 if ranges were skipped
    assert "GHSA-xxxx-yyyy" in cves[0]["detail"]

    from importlib.metadata import version
    assert version("requests") in cves[0]["detail"]      # the real installed version, not "2.31"


def test_cve_check_queries_every_installed_requirement(recon, manifest):
    """Names come from the manifest; versions from the environment.

    Covers the shapes a real requirements.txt throws at the parser: extras, environment
    markers, comments, blanks, and `-r`/`-e` lines (which name no package at all).
    """
    skill, _ = recon
    asked: list[dict] = []
    skill._osv = lambda pkgs: asked.extend(pkgs) or [[] for _ in pkgs]
    manifest("psycopg[binary]>=3.1\n"            # extra
             "requests>=2.31  # inline comment\n"
             "tzdata>=2024.1 ; sys_platform == 'win32'\n"   # environment marker
             "-r other-requirements.txt\n"       # names no package
             "-e .\n"
             "# a comment\n\n"
             "fastapi>=0.110\n")
    skill._check_dependencies()
    assert [p["package"]["name"] for p in asked] == ["psycopg", "requests", "tzdata", "fastapi"]
    assert all(p["version"] and p["package"]["ecosystem"] == "PyPI" for p in asked)


def test_uninstalled_requirement_is_skipped_not_guessed(recon, manifest):
    skill, _ = recon
    asked: list[dict] = []
    skill._osv = lambda pkgs: asked.extend(pkgs) or [[] for _ in pkgs]
    manifest("requests>=2.31\nnot-a-real-package-xyz>=1.0\n")
    skill._check_dependencies()
    assert [p["package"]["name"] for p in asked] == ["requests"]


def test_container_health_is_reported(mem, recon):
    skill, _ = recon
    skill._check_containers()
    found = {f["detail"] for f in mem.open_findings() if f["check_type"] == "container"}
    assert any("agentos-bot is errored" in d for d in found)
    assert any("unhealthy" in d for d in found)


# ================================================================= REPORT ONLY
def test_skill_exposes_no_mutating_action(recon):
    """Structurally incapable of acting: no restart/patch/fix command exists."""
    skill, _ = recon
    actions = set(skill.commands())
    assert not {"restart", "patch", "fix", "deploy", "kill", "update"} & actions


def test_scan_never_runs_a_mutating_command(recon):
    """Only read-only inspection commands may ever be shelled out."""
    skill, calls = recon
    skill.enroll(target="h.test", ports="80")
    skill.scan(notify=False)
    for cmd in calls["run"]:
        assert cmd[0] in ("pm2", "docker")
        assert not ({"restart", "stop", "start", "rm", "kill", "reload"} & set(cmd))


def test_contract_declares_report_only(recon):
    skill, _ = recon
    c = skill.contract()
    assert "report_only_never_acts" in c.hard_invariants
    assert "enrolled_targets_only" in c.hard_invariants
    assert c.reads_categories == []          # no instruction steers a security scan


# ================================================================= recurring escalation
def test_recurring_finding_escalates_instead_of_repeating(mem, recon):
    skill, _ = recon
    skill.enroll(target="h.test", ports="80,443")
    for _ in range(ESCALATE_AFTER):
        skill.scan(notify=False)
    rep = skill.report(notify=False)
    assert rep.data["recurring"] >= 1
    assert "RECURRING ×3" in rep.text
    # deduped, not duplicated
    tls = [f for f in mem.open_findings() if f["check_type"] == "tls_expiry"]
    assert len(tls) == 1 and tls[0]["occurrences"] == ESCALATE_AFTER


def test_fixed_finding_resolves_itself(mem, recon):
    skill, _ = recon
    skill.enroll(target="h.test", ports="80,443")
    skill.scan(notify=False)
    assert any(f["check_type"] == "exposed_file" for f in mem.open_findings())

    skill._http = lambda url: 404              # Calvin fixed it
    skill._now = lambda: NOW + DAY
    skill.scan(notify=False)
    assert not any(f["check_type"] == "exposed_file" for f in mem.open_findings())
    # resolved, not deleted (§0 P4)
    row = mem.execute("SELECT status FROM infra_scan_results WHERE check_type='exposed_file'").fetchone()
    assert row["status"] == "resolved"


def test_report_ranks_by_severity(mem, recon):
    skill, _ = recon
    skill.enroll(target="h.test", ports="80,443")
    skill.scan(notify=False)
    sevs = [f["severity"] for f in mem.open_findings()]
    assert sevs[0] == "critical"               # exposed .env first
    assert sevs == sorted(sevs, key=lambda s: ["critical", "high", "medium", "low", "info"].index(s))


def test_clean_report(recon):
    skill, _ = recon
    assert "nothing open" in skill.report(notify=False).text.lower()
