"""Email agent tests: classification, cleanup idempotency, never-trash, never-send.

All Gmail traffic is a MagicMock service; the LLM is the offline FakeLLM. These tests
enforce the §0 guardrails: bulk mail is archived (INBOX removed) not trashed, actionable
mail keeps its INBOX label, and reply drafting creates a DRAFT and NEVER calls send.
"""

from __future__ import annotations

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
