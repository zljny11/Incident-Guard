from __future__ import annotations

from incident_guard.sessions.session_store import SessionStore


def test_replay_returns_empty_list_for_missing_session(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path)

    assert store.replay("missing-session") == []


def test_multiple_sessions_do_not_pollute_each_other(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path)

    store.append_message("session-a", "user", "A message")
    store.append_message("session-b", "user", "B message")

    assert store.replay("session-a")[0]["content"] == "A message"
    assert store.replay("session-b")[0]["content"] == "B message"


def test_replay_limit_returns_latest_messages_without_truncating_file(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path)

    store.append_message("session-a", "user", "message 1")
    store.append_message("session-a", "assistant", "message 2")
    store.append_message("session-a", "user", "message 3")

    limited = store.replay("session-a", limit=2)
    full = store.replay("session-a")

    assert [message["content"] for message in limited] == ["message 2", "message 3"]
    assert [message["content"] for message in full] == [
        "message 1",
        "message 2",
        "message 3",
    ]


def test_replay_limit_zero_returns_empty_list(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path)

    store.append_message("session-a", "user", "message 1")

    assert store.replay("session-a", limit=0) == []


def test_list_sessions_returns_id_message_count_and_last_updated(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path)

    store.append_message("session-a", "user", "A message 1")
    store.append_message("session-a", "assistant", "A message 2")
    store.append_message("session-b", "user", "B message 1")

    summaries = store.list_sessions()
    summary_by_id = {summary.session_id: summary for summary in summaries}

    assert [summary.session_id for summary in summaries] == ["session-a", "session-b"]
    assert summary_by_id["session-a"].message_count == 2
    assert summary_by_id["session-b"].message_count == 1
    assert summary_by_id["session-a"].last_updated is not None
    assert summary_by_id["session-b"].last_updated is not None
