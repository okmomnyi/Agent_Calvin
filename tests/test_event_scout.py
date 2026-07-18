"""Event Scout tests: CTFtime/RSS/ICS parsers, free-only filter, dedupe, tag-match ranking,
physical-city bias, interest-tag editing, and Interested→semester-planner promotion."""

from __future__ import annotations

import json

from skills.event_scout.sources import (RawEvent, parse_ctftime, parse_ics_events,
                                        parse_rss_events, parse_event_date)
from skills.event_scout.skill import EventScoutSkill


NOW = 1_800_000_000.0

CTFTIME = json.dumps([
    {"id": 1, "title": "CyberQuest CTF", "start": "2027-06-01T00:00:00+00:00",
     "url": "https://ctf.example/1", "onsite": False, "location": ""},
    {"id": 2, "title": "Nairobi Onsite CTF", "start": "2027-06-10T00:00:00+00:00",
     "url": "https://ctf.example/2", "onsite": True, "location": "Nairobi, Kenya"},
])

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Kubernetes Meetup</title><link>https://m/1</link><guid>m1</guid>
<published>2027-06-05T18:00:00+00:00</published></item></channel></rss>"""

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-1
SUMMARY:DevOps Days Nairobi
DTSTART:20270620T090000Z
LOCATION:Nairobi
URL:https://devopsdays/nbo
END:VEVENT
END:VCALENDAR"""


# ------------------------------------------------------------------ parsers
def test_parse_ctftime_free_and_format():
    events = parse_ctftime(json.loads(CTFTIME))
    assert len(events) == 2
    assert all(e.free for e in events)
    assert events[0].fmt == "online"
    assert events[1].fmt == "physical" and "Nairobi" in events[1].location
    assert "ctf" in events[0].tags


def test_parse_rss_events():
    events = parse_rss_events(RSS, "meetup", ["devops"])
    assert events[0].title == "Kubernetes Meetup"
    assert events[0].tags == ["devops"]


def test_parse_ics_events():
    events = parse_ics_events(ICS, "devopsdays", ["devops"])
    assert events[0].title == "DevOps Days Nairobi"
    assert events[0].date == "2027-06-20"
    assert events[0].fmt == "physical" and events[0].location == "Nairobi"


def test_parse_event_date():
    assert parse_event_date("2027-06-01T00:00:00+00:00") is not None
    assert parse_event_date("2027-06-01") is not None
    assert parse_event_date("garbage") is None


# ------------------------------------------------------------------ skill
class _FakeSource:
    name = "fake"
    enabled = True

    def __init__(self, events):
        self._events = events

    def fetch(self):
        return self._events


def _skill(mem, events, now=NOW):
    return EventScoutSkill(memory=mem, sources=[_FakeSource(events)], clock=lambda: now)


def test_scan_stores_only_free_and_dedupes(mem):
    events = [
        RawEvent("s", "a", "Free CTF", date="2027-06-01", tags=["ctf"], free=True),
        RawEvent("s", "b", "Paid Conf", date="2027-06-02", tags=["devops"], free=False),  # dropped
    ]
    skill = _skill(mem, events)
    first = skill.scan()
    assert first.data["new"] == 1                    # paid one not stored
    assert skill.scan().data["new"] == 0             # dedupe on re-scan
    assert mem.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1


def test_find_ranks_by_tag_match(mem):
    events = [
        RawEvent("s", "a", "Baking Class", date="2027-06-05", tags=["cooking"], free=True),
        RawEvent("s", "b", "DevOps & Cloud Meetup", date="2027-06-05", tags=["devops", "cloud computing"], free=True),
    ]
    skill = _skill(mem, events)
    skill.scan()
    res = skill.find()
    titles = [e["title"] for e in res.data["events"]]
    assert titles[0] == "DevOps & Cloud Meetup"      # tag match ranks it first
    assert "Baking Class" not in titles              # zero tag match dropped (no filter)
    assert "WHEN" in res.text and "WHERE" in res.text and "LINK" in res.text
    assert res.data["events"][0]["local_time"]


def test_physical_bias_penalises_far_cities(mem):
    events = [
        RawEvent("s", "near", "Cloud Meetup", fmt="physical", location="Nairobi",
                 date="2027-06-05", tags=["cloud computing"], free=True),
        RawEvent("s", "far", "Cloud Meetup Berlin", fmt="physical", location="Berlin, Germany",
                 date="2027-06-05", tags=["cloud computing"], free=True),
    ]
    skill = _skill(mem, events)
    skill.scan()
    titles = [e["title"] for e in skill.find().data["events"]]
    assert titles[0] == "Cloud Meetup"               # Nairobi ranks above Berlin


def test_tags_add_remove_persist(mem):
    skill = _skill(mem, [])
    skill.tags(action="add", tag="quantum computing")
    assert "quantum computing" in skill.interest_tags()
    skill.tags(action="remove", tag="quantum computing")
    assert "quantum computing" not in skill.interest_tags()


def test_interested_promotes_to_planner(mem):
    events = [RawEvent("ctftime", "1", "CyberQuest CTF", date="2027-06-01", tags=["ctf"], free=True)]
    skill = _skill(mem, events)
    skill.scan()
    eid = mem.events_by_status("new")[0]["id"]
    skill.interested(event_id=eid)
    assert mem.get_event(eid)["status"] == "interested"
    # a planner deadline/event was created so it surfaces in the morning briefing
    deadlines = mem.deadlines_within(400, now=NOW)
    assert any("CyberQuest CTF" in d["title"] for d in deadlines)


def test_skip_marks_skipped(mem):
    events = [RawEvent("s", "x", "Some CTF", date="2027-06-01", tags=["ctf"], free=True)]
    skill = _skill(mem, events)
    skill.scan()
    eid = mem.events_by_status("new")[0]["id"]
    skill.skip(event_id=eid)
    assert mem.get_event(eid)["status"] == "skipped"


def test_cancelled_or_postponed_events_are_not_recommended(mem):
    events = [
        RawEvent("s", "x", "Cloud CTF POSTPONED", date="2027-06-01", tags=["ctf"]),
        RawEvent("s", "y", "Active Cloud CTF", date="2027-06-01", tags=["ctf"]),
    ]
    skill = _skill(mem, events)
    skill.scan()
    titles = [event["title"] for event in skill.find().data["events"]]
    assert titles == ["Active Cloud CTF"]
