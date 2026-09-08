"""End-to-end smoke test with NO network and NO secrets.

Runs the full pipeline against the in-memory repository and fake clients, and
asserts the critical invariants:
  * a second /poll does NOT resend an already-seen review;
  * a second "publish" tap does NOT publish twice (stale rejection);
  * the webhook update de-dup marks processed only once.

Usage:  python -m scripts.smoke_test
"""
from __future__ import annotations

from app.config import Settings
from app.domain.models import GenerationResult, make_doc_id
from app.services.pipeline import Deps, handle_update, run_poll
from app.services.prompt_service import PromptService
from app.services.repository import MemoryRepository


class FakeWB:
    def __init__(self, feedbacks):
        self._feedbacks = feedbacks
        self.published = []

    def iter_unanswered_feedbacks(self):
        return list(self._feedbacks)

    def publish_answer(self, feedback_id, text):
        self.published.append((feedback_id, text))
        return {"ok": True}


class FakeOpenAI:
    def __init__(self):
        self.calls = 0

    def generate_answer(self, system, user):
        self.calls += 1
        return GenerationResult(text=f"Ответ-вариант-{self.calls}", model="gpt-4.1-mini",
                                prompt_version="reviews_v1",
                                usage={"input_tokens": 10, "output_tokens": 5},
                                latency_ms=42, request_id="resp_test")


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self._id = 1000

    def send_message(self, chat_id, text, reply_markup=None):
        self._id += 1
        self.sent.append((chat_id, text))
        return {"message_id": self._id}

    def send_force_reply(self, chat_id, text):
        self._id += 1
        return {"message_id": self._id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return {"message_id": message_id}

    def answer_callback_query(self, cq_id, text=""):
        return {"ok": True}


class FakeBQ:
    def insert_event(self, event):
        return True

    def upsert_current(self, row):
        return True


def build_deps(feedbacks):
    settings = Settings(gcp_project_id="", telegram_chat_id="302044578",
                        telegram_allowed_user_ids={"302044578"}, wb_publish_enabled=True)
    return Deps(settings=settings, repo=MemoryRepository(), wb=FakeWB(feedbacks),
                openai=FakeOpenAI(), telegram=FakeTelegram(), bq=FakeBQ(),
                prompts=PromptService("prompts", "reviews_v1"))


def main() -> int:
    feedbacks = [{
        "id": "REVIEW_1", "productValuation": 5, "createdDate": "2026-07-20T10:00:00Z",
        "text": "Отличный крем!", "userName": "Анна",
        "productDetails": {"productName": "Крем для лица увлажняющий",
                           "supplierArticle": "438775437", "nmId": 111, "brandName": "EVETIS"}}]
    deps = build_deps(feedbacks)

    s1 = run_poll(deps)
    assert s1["processed"] == 1 and len(deps.telegram.sent) == 1

    s2 = run_poll(deps)
    assert s2["processed"] == 0 and s2["skipped"] == 1
    assert len(deps.telegram.sent) == 1, "review was resent!"

    doc_id = make_doc_id("wb", "review", "REVIEW_1")

    def cb(uid, action):
        return {"update_id": uid, "callback_query": {
            "id": f"cq{uid}", "data": f"{action}:{doc_id}", "from": {"id": 302044578},
            "message": {"message_id": 1001, "chat": {"id": 302044578}}}}

    # webhook-level de-dup
    assert deps.repo.begin_update(1) == "new"
    r = handle_update(deps, cb(1, "pub"))
    deps.repo.complete_update(1)
    assert r["status"] == "published"
    assert deps.wb.published == [("REVIEW_1", "Ответ-вариант-1")]
    assert deps.repo.begin_update(1) == "duplicate"

    # second publish tap -> stale, no double publish
    assert deps.repo.begin_update(2) == "new"
    r2 = handle_update(deps, cb(2, "pub"))
    assert r2["status"] == "stale" and len(deps.wb.published) == 1

    print("SMOKE TEST PASSED ✅")
    print("  poll#1:", s1, "poll#2:", s2, "published:", deps.wb.published)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
