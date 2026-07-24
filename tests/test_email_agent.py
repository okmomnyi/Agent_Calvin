"""Email agent tests: classification, cleanup idempotency, never-trash, never-send.

All Gmail traffic is a MagicMock service; the LLM is the offline FakeLLM. These tests
enforce the §0 guardrails: bulk mail is archived (INBOX removed) not trashed, actionable
mail keeps its INBOX label, and reply drafting creates a DRAFT and NEVER calls send.
"""

from __future__ import annotations

import pytest

from unittest.mock import MagicMock


from core.gmail_client import GmailClient
from skills.email_agent import EmailAgentSkill, _gmail_query


def _message(msg_id: str = "m1", subject: str = "50% OFF today", sender: str = "deals@shop.com"):
    return {
        "id": msg_id,
        "threadId": "t1",
        "snippet": "Huge sale ends tonight",
        "payload": {"headers": [
            {"name": "Subject", "value": subject},
            {"name": "From", "value": sender},
            {"name": "Message-ID", "value": "<abc@shop.com>"},
        ]},
    }


def _mock_service(message: dict) -> MagicMock:
    svc = MagicMock()
    users = svc.users.return_value
    messages = users.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": message["id"]}]}
    messages.get.return_value.execute.return_value = message
    messages.modify.return_value.execute.return_value = {}
    labels = users.labels.return_value
    labels.list.return_value.execute.return_value = {"labels": []}
    labels.create.return_value.execute.return_value = {"id": "LBL_X"}
    users.drafts.return_value.create.return_value.execute.return_value = {"id": "draft1"}
    return svc


def _skill(service: MagicMock, fake_llm, mem) -> EmailAgentSkill:
    return EmailAgentSkill(gmail=GmailClient(service=service), llm=fake_llm, memory=mem)


def test_classify_email_returns_valid_category(fake_llm, mem):
    fake_llm.classify_result = "promotion"
    skill = _skill(_mock_service(_message()), fake_llm, mem)
    cat = skill.classify_email("50% off", "deals@x.com", "buy now")
    assert cat == "promotion"


def test_cleanup_archives_promotion_never_trashes(fake_llm, mem):
    fake_llm.classify_result = "promotion"
    svc = _mock_service(_message())
    skill = _skill(svc, fake_llm, mem)

    result = skill.cleanup()
    assert result.ok and result.data["processed"] == 1

    messages = svc.users.return_value.messages.return_value
    # archived => INBOX removed
    body = messages.modify.call_args.kwargs["body"]
    assert "INBOX" in body["removeLabelIds"]
    # NEVER trashed
    assert messages.trash.called is False
    # recorded as archived
    row = mem.execute("SELECT * FROM emails WHERE gmail_id='m1'").fetchone()
    assert row["category"] == "promotion"
    assert row["action"] == "archived"


def test_cleanup_labels_important_keeps_inbox(fake_llm, mem):
    fake_llm.classify_result = "important"
    svc = _mock_service(_message(subject="Contract to sign", sender="client@corp.com"))
    skill = _skill(svc, fake_llm, mem)

    skill.cleanup()
    messages = svc.users.return_value.messages.return_value
    body = messages.modify.call_args.kwargs["body"]
    assert "addLabelIds" in body
    assert "removeLabelIds" not in body  # important mail stays in the inbox
    row = mem.execute("SELECT * FROM emails WHERE gmail_id='m1'").fetchone()
    assert row["action"] == "labelled"


def test_cleanup_is_idempotent(fake_llm, mem):
    fake_llm.classify_result = "promotion"
    # pre-record the message as already processed
    mem.record_email("m1", category="promotion", action="archived")
    svc = _mock_service(_message())
    skill = _skill(svc, fake_llm, mem)

    result = skill.cleanup()
    assert result.data["processed"] == 0
    # already-seen message is never fetched or modified again
    assert svc.users.return_value.messages.return_value.get.called is False


# ------------------------------------------------------------------ search (regression, #9)
# Telegram log: "List all emails from LinkedIn" -> "Inbox cleanup done. Processed 0 new
# message(s)" -- there was no read-only listing action, so the catalogue router's best guess
# was `cleanup`, which ran a real inbox pass and reported on THAT instead of answering the
# question. search() must never archive/label/trash anything it looks at.
def test_search_by_sender_lists_matches_without_mutating_anything(fake_llm, mem):
    svc = _mock_service(_message(subject="10 people viewed your profile", sender="LinkedIn <no-reply@linkedin.com>"))
    skill = _skill(svc, fake_llm, mem)

    result = skill.search(sender="LinkedIn")
    assert result.data["count"] == 1
    assert "LinkedIn" in result.text
    assert "10 people viewed your profile" in result.text
    # read-only: no archive/label/trash call of any kind
    messages = svc.users.return_value.messages.return_value
    assert messages.modify.called is False


def test_search_with_no_matches_says_so_honestly(fake_llm, mem):
    svc = MagicMock()
    svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {}
    skill = _skill(svc, fake_llm, mem)

    result = skill.search(sender="nobody-like-this")
    assert result.data["count"] == 0
    assert "no emails found" in result.text.lower()


def test_search_with_neither_query_nor_sender_asks_rather_than_guessing(fake_llm, mem):
    skill = _skill(_mock_service(_message()), fake_llm, mem)
    result = skill.search()
    assert result.ok is False


def test_search_gmail_failure_is_honest_not_a_crash(fake_llm, mem):
    svc = MagicMock()
    svc.users.return_value.messages.return_value.list.return_value.execute.side_effect = RuntimeError("down")
    skill = _skill(svc, fake_llm, mem)
    result = skill.search(sender="LinkedIn")
    assert result.ok is False
    assert "couldn't reach gmail" in result.text.lower()


def test_draft_creates_draft_and_never_sends(fake_llm, mem):
    fake_llm.post_result = "Thanks — I'll get back to you tomorrow.\nCalvin"
    svc = _mock_service(_message(subject="Quick question", sender="Jane <jane@corp.com>"))
    skill = _skill(svc, fake_llm, mem)

    result = skill.draft(instruction="tell her I'll reply tomorrow", msg_id="m1")
    assert result.ok
    users = svc.users.return_value
    assert users.drafts.return_value.create.called is True     # draft created
    assert users.messages.return_value.send.called is False    # NOTHING sent (§0)
    assert result.data["to"] == "jane@corp.com"


def test_gmail_client_exposes_no_send_method():
    # Guardrail at the API surface: the client can draft but structurally cannot send.
    assert not hasattr(GmailClient, "send")
    assert not hasattr(GmailClient, "send_message")
    assert hasattr(GmailClient, "create_draft")


def test_digest_groups_and_returns_counts(fake_llm, mem):
    fake_llm.post_result = "- Contract to sign — needs your signature"
    mem.record_email("a", category="important", subject="Contract", sender="c@x.com", action="labelled")
    mem.record_email("b", category="promotion", subject="Sale", sender="s@x.com", action="archived")
    mem.record_email("c", category="personal", subject="Lunch?", sender="pal@x.com", action="labelled")
    skill = EmailAgentSkill(gmail=None, llm=fake_llm, memory=mem)

    result = skill.digest(notify=False)
    assert result.data["action_needed"] == 1
    assert result.data["ignored"] == 1     # the promotion
    assert "ACTION NEEDED" in result.text


# Regression (#15): the interactive dispatch path ("summarize my inbox" via Telegram) calls
# digest() with no explicit `notify=`, so the METHOD's own default decides. It used to
# default to True, double-sending: once via the reply channel echoing CommandResult.text,
# once via digest()'s own internal notify() call -- "sent twice, identical, same minute" in
# the log. manage.py's CLI is unaffected: it always passes an explicit notify= value.
def test_digest_default_does_not_double_send(fake_llm, mem):
    notify = MagicMock()
    skill = EmailAgentSkill(gmail=None, llm=fake_llm, memory=mem, notify=notify)
    result = skill.digest()  # no explicit notify= -- exactly how interactive dispatch calls it
    assert "Nothing new" in result.text or "digest" in result.text.lower()
    notify.assert_not_called()


def test_trash_starts_with_a_question_and_changes_nothing(fake_llm, mem):
    svc = _mock_service(_message())
    skill = _skill(svc, fake_llm, mem)
    result = skill.request_trash()
    assert result.data["awaiting"] == "email_query"
    assert svc.users.return_value.messages.return_value.trash.called is False


def test_trash_preview_requires_exact_confirmation(fake_llm, mem):
    svc = _mock_service(_message())
    skill = _skill(svc, fake_llm, mem)
    preview = skill.request_trash(query="from deals@shop.com")
    assert preview.data["requires_confirmation"] is True
    messages = svc.users.return_value.messages.return_value
    assert messages.list.call_args.kwargs["q"] == "from:(deals@shop.com)"
    assert messages.trash.called is False

    refused = skill.continue_trash(text="yes")
    assert refused.ok is False
    assert messages.trash.called is False


def test_confirmed_trash_is_recoverable_and_audited(fake_llm, mem):
    svc = _mock_service(_message())
    skill = _skill(svc, fake_llm, mem)
    skill.request_trash(query="promotions")
    result = skill.continue_trash(text="confirm trash")
    messages = svc.users.return_value.messages.return_value
    assert result.ok is True
    messages.trash.assert_called_once_with(userId="me", id="m1")
    assert mem.execute("SELECT action FROM emails WHERE gmail_id='m1'").fetchone()["action"] == "trashed"

    restored = skill.restore_last()
    assert restored.ok is True
    messages.untrash.assert_called_once_with(userId="me", id="m1")
    assert mem.execute("SELECT action FROM emails WHERE gmail_id='m1'").fetchone()["action"] == "restored"


def test_spoken_email_filters_are_deterministic():
    assert _gmail_query("older than 30 days") == "older_than:30d"
    assert _gmail_query("subject newsletter") == "subject:(newsletter)"
    assert _gmail_query("promotions") == "category:promotions"


# ================================================================= compose + confirmed send
def test_compose_previews_and_sends_nothing_until_confirmed(fake_llm, mem):
    """Calvin authorized a test send, but sending is two-step by design (§0 P3)."""
    from unittest.mock import MagicMock

    mailer = MagicMock()
    skill = EmailAgentSkill(gmail=GmailClient(service=_mock_service(_message())),
                            llm=fake_llm, memory=mem, mailer=mailer)
    res = skill.compose(to="okmomanyi56@gmail.com", body="Deploy is live.")
    assert res.data["requires_confirmation"] is True
    assert "not sent yet" in res.text.lower()
    mailer.send_email.assert_not_called()          # nothing sent on compose


def test_confirm_send_actually_sends(fake_llm, mem):
    from unittest.mock import MagicMock

    mailer = MagicMock()
    skill = EmailAgentSkill(gmail=GmailClient(service=_mock_service(_message())),
                            llm=fake_llm, memory=mem, mailer=mailer)
    skill.compose(to="okmomanyi56@gmail.com", subject="Test", body="Hello from AgentOS.")
    out = skill.continue_send(text="confirm send")
    mailer.send_email.assert_called_once()
    kw = mailer.send_email.call_args.kwargs
    assert kw["to"] == "okmomanyi56@gmail.com" and kw["body"] == "Hello from AgentOS."
    assert "Sent" in out.text


def test_cancel_send_sends_nothing(fake_llm, mem):
    from unittest.mock import MagicMock

    mailer = MagicMock()
    skill = EmailAgentSkill(gmail=GmailClient(service=_mock_service(_message())),
                            llm=fake_llm, memory=mem, mailer=mailer)
    skill.compose(to="okmomanyi56@gmail.com", body="Hi.")
    out = skill.continue_send(text="no wait, cancel")
    mailer.send_email.assert_not_called()
    assert "ancel" in out.text


def test_compose_parses_recipient_from_a_spoken_instruction(fake_llm, mem):
    from unittest.mock import MagicMock

    mailer = MagicMock()
    fake_llm.post_result = "Body drafted by the model."
    skill = EmailAgentSkill(gmail=GmailClient(service=_mock_service(_message())),
                            llm=fake_llm, memory=mem, mailer=mailer)
    res = skill.compose(instruction="to okmomanyi56@gmail.com saying the deploy is live")
    assert res.data["to"] == "okmomanyi56@gmail.com"
    assert res.data["requires_confirmation"] is True


def test_reply_drafting_is_still_draft_only():
    """The new send path must not have loosened the reply path -- GmailClient still can't send."""
    assert not hasattr(GmailClient, "send")
    assert not hasattr(GmailClient, "send_message")


def test_delete_by_sender_uses_from_not_full_text(fake_llm, mem):
    """On a delete, "linkedin" must mean FROM LinkedIn, not any mail mentioning it.

    A live preview of "linkedin" as full text also matched OKX and Railway (footer links),
    which would then be trashed on confirm. from:() keys on the sender instead.
    """
    from skills.email_agent import _gmail_query

    assert _gmail_query("linkedin") == "from:(linkedin)"
    assert _gmail_query("okx") == "from:(okx)"
    # a genuine content query keeps full-text so it still works
    assert _gmail_query("the invoice from acme corp") == "the invoice from acme corp"
    # categories still win
    assert _gmail_query("promotions") == "category:promotions"


# ============================================================ compound delete requests
@pytest.mark.parametrize("said,expected", [
    # The exact shape that silently failed: a real request to clear LinkedIn mail became
    # from:(LinkedIn promotional) -- no such sender -- so it reported "No emails matched"
    # while the inbox was full of them.
    ("LinkedIn promotional emails", "from:(LinkedIn) category:promotions"),
    ("all LinkedIn emails and Facebook emails", "from:(LinkedIn OR Facebook)"),
    ("LinkedIn, Facebook and Indy games emails", "from:(LinkedIn OR Facebook OR Indy games)"),
    ("all the emails related to okx that are not transactional", "from:(okx)"),
    # single-term and structured forms must keep working
    ("linkedin", "from:(linkedin)"),
    ("promotional emails", "category:promotions"),
    ("from linkedin", "from:(linkedin)"),
    ("older than 30 days", "older_than:30d"),
    ("subject newsletter", "subject:(newsletter)"),
])
def test_spoken_delete_requests_become_precise_gmail_queries(said, expected):
    from skills.email_agent import _clean_delete_query, _gmail_query

    assert _gmail_query(_clean_delete_query(said)) == expected


def test_delete_queries_target_the_sender_not_the_body():
    """Full-text 'linkedin' also matches OKX/Railway (their footers link to LinkedIn).

    On a path whose next step deletes what matched, over-matching is the dangerous failure.
    """
    from skills.email_agent import _clean_delete_query, _gmail_query

    q = _gmail_query(_clean_delete_query("linkedin"))
    assert q.startswith("from:"), f"{q} would match any message mentioning linkedin"
