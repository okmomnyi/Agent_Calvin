"""Phone book (Phase 36 Slice 3) — server-side contacts, and the only way
skills/phone.py (Slice 5) turns a spoken name into a number to dial.

Structural rules, not policy:

* Numbers are normalized to E.164 on write via `phonenumbers` (Google's libphonenumber
  port — offline metadata, no network call), using `phone.default_region` from config
  (default KE / +254). There is no hardcoded country code anywhere in this module.
  Un-normalizable input is REJECTED with a clear error rather than stored as junk that
  would silently fail the moment something tries to dial it.
* Lookup is fuzzy on name but returns ALL matches. One match proceeds; more than one is
  refused with the candidate list so the caller — a skill, the CLI, Calvin himself — can
  say which. This module never indexes `[0]`. Calling the wrong person is unacceptable.
* Retire, never delete (§0 P4). A retired contact stays queryable with
  `include_retired=True`; nothing here exposes a delete path.
* Contacts are PII: `find`/`list` return exactly what was asked for and nothing gets
  bulk-dumped into an LLM prompt or a semantic-index embedding by this module. Lookups log
  a contact's *id*, never its name or number — see the `log.info` calls below.
"""

from __future__ import annotations

import csv
import io
import re
import time
from typing import Any, Callable

import phonenumbers

from core.config import get_settings
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import BaseSkill, CommandResult, SkillContract

log = get_logger("skills.contacts")


def normalize_phone(raw: str, region: str | None = None) -> str:
    """Return E.164 (e.g. +2547...) or raise ValueError with a message safe to show Calvin."""
    region = region or get_settings().get("phone", "default_region", default="KE")
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("No phone number given.")
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"\"{raw}\" doesn't look like a phone number ({exc})") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"\"{raw}\" isn't a valid number for region {region}.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class ContactsSkill(BaseSkill):
    name = "contacts"

    def __init__(self, memory: Memory | None = None,
                 settings_fn: Callable[[], Any] | None = None) -> None:
        self._mem = memory
        self._settings = settings_fn or get_settings

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"add": self.add, "list": self.list_contacts, "find": self.find,
                "import_file": self.import_file, "retire": self.retire}

    def contract(self) -> SkillContract:
        # Reads nothing: a standing instruction has no business steering who the phone
        # book resolves a name to.
        return SkillContract(reads_categories=[])

    def _region(self) -> str:
        return self._settings().get("phone", "default_region", default="KE")

    # ------------------------------------------------------------- add
    def add(self, name: str = "", phone: str = "", email: str = "", notes: str = "",
            source: str = "manual", **_: Any) -> CommandResult:
        name = (name or "").strip()
        if not name:
            return CommandResult(text="A contact needs a name.", ok=False)
        e164 = None
        if phone:
            try:
                e164 = normalize_phone(phone, self._region())
            except ValueError as exc:
                return CommandResult(text=str(exc), ok=False)
        now = time.time()
        with self.mem.tx() as conn:
            row = conn.execute(
                "INSERT INTO contacts(name, phone_e164, email, notes, source, created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (name, e164, email or None, notes or None, source, now)).fetchone()
        log.info("contact added id=%s", row["id"])
        return CommandResult(text=f"Added {name}.", data={"id": row["id"], "phone_e164": e164})

    # ------------------------------------------------------------- list
    def list_contacts(self, include_retired: bool = False, **_: Any) -> CommandResult:
        sql = "SELECT * FROM contacts"
        if not include_retired:
            sql += " WHERE retired_at IS NULL"
        sql += " ORDER BY name"
        rows = [dict(r) for r in self.mem.execute(sql).fetchall()]
        if not rows:
            return CommandResult(text="No contacts yet.", data={"contacts": []})
        lines = [r["name"] + (f" — {r['phone_e164']}" if r["phone_e164"] else "")
                 for r in rows]
        return CommandResult(text="\n".join(lines), data={"contacts": rows})

    # ------------------------------------------------------------- find
    def find(self, name: str = "", include_retired: bool = False, **_: Any) -> CommandResult:
        matches = self._search(name, include_retired=include_retired)
        if not matches:
            return CommandResult(text=f"No contact matches \"{name}\".", ok=False,
                                 data={"matches": []})
        if len(matches) > 1:
            listed = ", ".join(f"{m['name']} ({m['phone_e164'] or 'no number'})"
                               for m in matches)
            return CommandResult(
                text=f"More than one contact matches \"{name}\": {listed}. Which one?",
                ok=False, data={"matches": matches, "ambiguous": True})
        m = matches[0]
        log.info("contact resolved id=%s", m["id"])
        return CommandResult(text=f"{m['name']}: {m['phone_e164'] or 'no number on file'}",
                             data={"contact": m})

    def _search(self, name: str, *, include_retired: bool = False) -> list[dict[str, Any]]:
        name = (name or "").strip()
        if not name:
            return []
        sql = "SELECT * FROM contacts WHERE name ILIKE %s"
        params: list[Any] = [f"%{name}%"]
        if not include_retired:
            sql += " AND retired_at IS NULL"
        sql += " ORDER BY name"
        return [dict(r) for r in self.mem.execute(sql, params).fetchall()]

    # ------------------------------------------------------------- retire
    def retire(self, name: str = "", contact_id: int | None = None, **_: Any) -> CommandResult:
        if contact_id is None:
            matches = self._search(name)
            if not matches:
                return CommandResult(text=f"No contact matches \"{name}\".", ok=False)
            if len(matches) > 1:
                listed = ", ".join(m["name"] for m in matches)
                return CommandResult(
                    text=f"More than one contact matches \"{name}\": {listed}. Which one?",
                    ok=False, data={"matches": matches})
            contact_id = matches[0]["id"]
        with self.mem.tx() as conn:
            cur = conn.execute(
                "UPDATE contacts SET retired_at=%s WHERE id=%s AND retired_at IS NULL",
                (time.time(), contact_id))
        if cur.rowcount == 0:
            return CommandResult(text="That contact is already retired (or doesn't exist).",
                                 ok=False)
        log.info("contact retired id=%s", contact_id)
        return CommandResult(text="Retired.", data={"id": contact_id})

    # ------------------------------------------------------------- import
    def import_file(self, path: str = "", **_: Any) -> CommandResult:
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return CommandResult(text=f"{path} not found.", ok=False)
        suffix = p.suffix.lower()
        text = p.read_text(encoding="utf-8", errors="replace")
        if suffix == ".vcf":
            entries = parse_vcard(text)
        elif suffix == ".csv":
            entries = parse_csv(text)
        else:
            return CommandResult(text="Only .csv and .vcf imports are supported.", ok=False)
        return self._import_entries(entries)

    def _import_entries(self, entries: list[dict[str, str]]) -> CommandResult:
        added, skipped, rejected = 0, 0, []
        region = self._region()
        for entry in entries:
            name = entry.get("name", "").strip()
            if not name:
                continue
            phone_raw = entry.get("phone", "").strip()
            e164 = None
            if phone_raw:
                try:
                    e164 = normalize_phone(phone_raw, region)
                except ValueError as exc:
                    rejected.append(f"{name}: {exc}")
                    continue
            # Idempotent: re-importing the same file must not duplicate. Same name + same
            # (possibly absent) number counts as the same contact already on file.
            existing = self.mem.execute(
                "SELECT id FROM contacts WHERE name=%s AND phone_e164 IS NOT DISTINCT FROM %s",
                (name, e164)).fetchone()
            if existing:
                skipped += 1
                continue
            with self.mem.tx() as conn:
                conn.execute(
                    "INSERT INTO contacts(name, phone_e164, email, notes, source, created_at) "
                    "VALUES(%s,%s,%s,%s,'import',%s)",
                    (name, e164, entry.get("email") or None, entry.get("notes") or None,
                     time.time()))
            added += 1
        lines = [f"Imported {added}, skipped {skipped} already on file."]
        if rejected:
            lines.append(f"{len(rejected)} rejected: " + "; ".join(rejected[:5]))
        return CommandResult(text=" ".join(lines),
                             data={"added": added, "skipped": skipped, "rejected": rejected})


def parse_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
        out.append({
            "name": lower.get("name") or lower.get("full name") or "",
            "phone": lower.get("phone") or lower.get("phone_e164") or lower.get("tel") or "",
            "email": lower.get("email") or "",
            "notes": lower.get("notes") or "",
        })
    return out


_VCARD_LINE = re.compile(r"^([A-Za-z][\w.-]*)(;[^:]*)?:(.*)$")


def parse_vcard(text: str) -> list[dict[str, str]]:
    """Minimal vCard 3.0/4.0 reader covering FN, TEL, EMAIL, NOTE — the subset AgentOS
    actually uses. Deliberately not a full RFC 6350 parser (no line-folding, no multi-value
    TYPE params): a real vCard library is a heavier dependency than this narrow need
    justifies, and a contact this can't parse is skipped, not guessed at.
    """
    out: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line.strip().upper() == "BEGIN:VCARD":
            current = {}
            continue
        if line.strip().upper() == "END:VCARD":
            if current.get("name"):
                out.append(current)
            continue
        m = _VCARD_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1).upper(), m.group(3)
        value = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        if key == "FN":
            current["name"] = value
        elif key == "TEL" and "phone" not in current:
            current["phone"] = value
        elif key == "EMAIL" and "email" not in current:
            current["email"] = value
        elif key == "NOTE":
            current["notes"] = value
    return out


SKILL = ContactsSkill()
