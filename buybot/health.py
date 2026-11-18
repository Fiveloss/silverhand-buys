"""Health from local timestamps and queue state; no RPC or Telegram requests."""

import time

from .i18n import t


def snapshot(app, now: float | None = None, chat_id: int | None = None) -> dict:
    now = time.time() if now is None else now
    w, s = app.watcher, app.sender

    def age(obj, field):
        value = getattr(obj, field, None)
        return max(0, int(now - value)) if value is not None else None

    last = app.db.last_block()
    head = getattr(w, "head", None)
    return {"rpc_age": age(w, "last_rpc_ok"), "watcher_age": age(w, "last_ok"),
            "head_age": age(w, "last_head_change"), "sender_age": age(s, "last_ok"),
            "sent_age": age(s, "last_sent"), "lag": max(0, head - last)
            if head is not None and last is not None else None,
            "outbox": app.db.outbox_size(chat_id), "queue_age": int(app.db.oldest_outbox_age(now, chat_id))}


def issues(app, *, now: float | None = None, stale_after: float = 300, tasks=(),
           chat_id: int | None = None) -> set[str]:
    state = snapshot(app, now, chat_id)
    found = set()
    for field, code in (("rpc_age", "rpc"), ("head_age", "head"),
                        ("watcher_age", "watcher"), ("sender_age", "sender")):
        if state[field] is not None and state[field] > stale_after:
            found.add(code)
    if state["queue_age"] > stale_after:
        found.add("queue")
    if any(task.done() and (task.get_name() != "backfill" or task.cancelled() or task.exception() is not None)
           for task in tasks):
        found.add("task")
    return found


def report(app, lang="en", chat_id: int | None = None) -> str:
    state = snapshot(app, chat_id=chat_id)
    data = {key: "—" if value is None else value for key, value in state.items()}
    return t(lang, "health_chain", **data) + "\n" + t(lang, "health_sender", **data)
