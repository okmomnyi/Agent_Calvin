"""Phone book (Phase 36 Slice 3).

The properties that matter: numbers always end up E.164 (never a hardcoded country code —
`phone.default_region` drives it), an ambiguous name ASKS instead of guessing `[0]`, and a
retired contact is never actually deleted (§0 P4). Contacts are PII: nothing here should log
a name or number, only an id.
"""

from __future__ import annotations

import logging

import pytest

from skills.contacts import ContactsSkill, normalize_phone, parse_csv, parse_vcard


class _Settings:
    def __init__(self, region: str = "KE") -> None:
        self._region = region

    def get(self, *keys, default=None):
        return self._region if keys == ("phone", "default_region") else default


@pytest.fixture
def contacts(mem):
    return ContactsSkill(memory=mem, settings_fn=lambda: _Settings())


# ================================================================= E.164 normalization
def test_local_kenyan_format_normalizes_to_e164():
    assert normalize_phone("0712345678", "KE") == "+254712345678"


def test_already_e164_stays_e164():
    assert normalize_phone("+254712345678", "KE") == "+254712345678"


def test_a_different_region_changes_how_a_bare_number_resolves():
    # Same digits, different default region -- proving there's no hardcoded country code.
    us_number = normalize_phone("2025550123", "US")
    assert us_number.startswith("+1")


def test_unnormalizable_input_is_rejected_with_a_clear_error():
    with pytest.raises(ValueError, match="doesn't look like|isn't a valid"):
        normalize_phone("not a phone number", "KE")


def test_empty_input_is_rejected():
    with pytest.raises(ValueError):
        normalize_phone("", "KE")


# ================================================================= add
def test_add_requires_a_name(contacts):
    result = contacts.add(name="", phone="0712345678")
    assert result.ok is False


def test_add_rejects_an_unnormalizable_number_rather_than_storing_junk(contacts):
    result = contacts.add(name="Mum", phone="garbage")
    assert result.ok is False
    row = contacts.mem.execute("SELECT * FROM contacts WHERE name='Mum'").fetchone()
    assert row is None, "a rejected number must never reach the table"


def test_add_normalizes_and_stores_e164(contacts):
    result = contacts.add(name="Mum", phone="0712345678")
    assert result.ok is True
    assert result.data["phone_e164"] == "+254712345678"


def test_add_without_a_phone_is_allowed(contacts):
    """A contact can exist with just a name/email/notes -- nothing invents a number."""
    result = contacts.add(name="No Number Yet", email="x@example.com")
    assert result.ok is True
    assert result.data["phone_e164"] is None


# ================================================================= find (never [0])
def test_find_with_zero_matches_says_so(contacts):
    result = contacts.find(name="Nobody")
    assert result.ok is False
    assert result.data["matches"] == []


def test_find_with_one_match_proceeds(contacts):
    contacts.add(name="Mum", phone="0712345678")
    result = contacts.find(name="Mum")
    assert result.ok is True
    assert result.data["contact"]["name"] == "Mum"


def test_find_with_multiple_matches_asks_and_never_guesses(contacts):
    contacts.add(name="John Doe", phone="0712345678")
    contacts.add(name="John Smith", phone="0712345679")
    result = contacts.find(name="John")
    assert result.ok is False
    assert result.data["ambiguous"] is True
    names = {m["name"] for m in result.data["matches"]}
    assert names == {"John Doe", "John Smith"}, "both candidates must be returned, not just [0]"


def test_retired_contacts_are_excluded_from_find_by_default(contacts):
    contacts.add(name="Old Contact", phone="0712345678")
    contacts.retire(name="Old Contact")
    result = contacts.find(name="Old Contact")
    assert result.ok is False
    result = contacts.find(name="Old Contact", include_retired=True)
    assert result.ok is True


# ================================================================= retire (never delete)
def test_retire_does_not_delete_the_row(contacts):
    added = contacts.add(name="Landlord", phone="0712345678")
    contacts.retire(name="Landlord")
    row = contacts.mem.execute(
        "SELECT * FROM contacts WHERE id=%s", (added.data["id"],)).fetchone()
    assert row is not None, "§0 P4: retire must never delete the row"
    assert row["retired_at"] is not None


def test_retiring_an_already_retired_contact_is_reported_not_silently_ok(contacts):
    contacts.add(name="Landlord", phone="0712345678")
    contacts.retire(name="Landlord")
    result = contacts.retire(name="Landlord")
    assert result.ok is False


def test_list_excludes_retired_unless_asked(contacts):
    contacts.add(name="Active", phone="0712345678")
    contacts.add(name="Gone", phone="0712345679")
    contacts.retire(name="Gone")
    assert [c["name"] for c in contacts.list_contacts().data["contacts"]] == ["Active"]
    all_names = {c["name"] for c in
                contacts.list_contacts(include_retired=True).data["contacts"]}
    assert all_names == {"Active", "Gone"}


# ================================================================= CSV / vCard import
def test_csv_import_is_idempotent(contacts):
    csv_text = "name,phone,email\nJane Doe,0712345678,jane@example.com\n"
    entries = parse_csv(csv_text)
    r1 = contacts._import_entries(entries)
    r2 = contacts._import_entries(entries)
    assert r1.data["added"] == 1
    assert r2.data["added"] == 0 and r2.data["skipped"] == 1
    rows = contacts.mem.execute("SELECT * FROM contacts WHERE name='Jane Doe'").fetchall()
    assert len(rows) == 1, "re-importing the same file must not duplicate"


def test_csv_import_rejects_bad_numbers_without_dropping_the_whole_batch(contacts):
    csv_text = "name,phone\nGood One,0712345678\nBad One,not-a-number\n"
    result = contacts._import_entries(parse_csv(csv_text))
    assert result.data["added"] == 1
    assert len(result.data["rejected"]) == 1
    assert contacts.mem.execute(
        "SELECT * FROM contacts WHERE name='Good One'").fetchone() is not None


def test_vcard_parses_fn_tel_email():
    vcf = (
        "BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nTEL;TYPE=CELL:0712345678\n"
        "EMAIL:jane@example.com\nEND:VCARD\n"
    )
    entries = parse_vcard(vcf)
    assert entries == [{"name": "Jane Doe", "phone": "0712345678", "email": "jane@example.com"}]


def test_vcard_import_is_idempotent(contacts):
    vcf = "BEGIN:VCARD\nFN:Jane Doe\nTEL:0712345678\nEND:VCARD\n"
    entries = parse_vcard(vcf)
    contacts._import_entries(entries)
    result = contacts._import_entries(entries)
    assert result.data["added"] == 0 and result.data["skipped"] == 1


# ================================================================= PII: never logged in full
def test_lookups_log_an_id_never_a_name_or_number(contacts, caplog):
    added = contacts.add(name="Secret Person", phone="0712345678")
    with caplog.at_level(logging.INFO, logger="skills.contacts"):
        contacts.find(name="Secret")
    messages = " ".join(r.message for r in caplog.records)
    assert "Secret Person" not in messages
    assert "+254712345678" not in messages
    assert str(added.data["id"]) in messages
