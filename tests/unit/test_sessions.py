"""Unit tests: fintrust/sessions.py, the sign-in sessions kept in DynamoDB (moto)."""

import pytest

from fintrust import sessions

pytestmark = pytest.mark.real_sessions

USER = "t.nkosi"
ROLES = ("compliance-officer",)


@pytest.fixture
def clock(monkeypatch):
    """Control the time the module sees, in whole seconds."""

    class Clock:
        now = 1_791_000_000

        def advance(self, seconds):
            self.now += seconds

    moment = Clock()
    monkeypatch.setattr(sessions, "_now", lambda: moment.now)
    return moment


@pytest.fixture
def table(sessions_table, clock):
    return sessions_table


def stored(table, session_id="s-1"):
    return table.get_item(Key={"session_id": session_id}).get("Item")


class TestStart:
    def test_records_who_signed_in_when_and_their_role(self, table, clock):
        item, created = sessions.start("s-1", USER, ROLES, "Thabo Nkosi", "Mozilla/5.0", clock.now - 5)
        assert created is True
        row = stored(table)
        assert row["username"] == USER and row["display_name"] == "Thabo Nkosi"
        assert row["roles"] == ["compliance-officer"] and row["status"] == "ACTIVE"
        assert row["started_epoch"] == clock.now - 5 and row["last_seen_epoch"] == clock.now
        assert row["ends_epoch"] == clock.now - 5 + 8 * 3600            # session_max_hours
        assert row["expires_at"] == clock.now - 5 + 90 * 86400          # DynamoDB time-to-live: 90 days

    def test_starting_the_same_sign_in_twice_returns_it_unchanged(self, table, clock):
        sessions.start("s-1", USER, ROLES)
        clock.advance(30)
        item, created = sessions.start("s-1", USER, ROLES)
        assert created is False and item["started_epoch"] == clock.now - 30

    def test_an_ended_session_can_never_be_started_again(self, table):
        """Otherwise a token copied before sign-out could simply reopen its session."""
        sessions.start("s-1", USER, ROLES)
        sessions.end("s-1", USER, sessions.SIGNED_OUT)
        with pytest.raises(sessions.SessionEnded) as ended:
            sessions.start("s-1", USER, ROLES)
        assert ended.value.reason == sessions.SIGNED_OUT

    def test_a_sign_in_older_than_the_limit_cannot_start(self, table, clock):
        with pytest.raises(sessions.SessionEnded) as ended:
            sessions.start("s-1", USER, ROLES, signed_in_epoch=clock.now - 9 * 3600)
        assert ended.value.reason == sessions.EXPIRED and stored(table) is None

    def test_a_sign_in_time_in_the_future_is_not_trusted(self, table, clock):
        item, _ = sessions.start("s-1", USER, ROLES, signed_in_epoch=clock.now + 3600)
        assert item["started_epoch"] == clock.now

    def test_no_token_password_or_long_user_agent_is_stored(self, table):
        sessions.start("s-1", USER, ROLES, user_agent="x" * 500)
        row = stored(table)
        assert len(row["user_agent"]) == sessions.USER_AGENT_LENGTH
        assert not {"token", "id_token", "password", "email"} & set(row)


class TestRequireActive:
    def test_an_open_session_is_returned(self, table):
        sessions.start("s-1", USER, ROLES)
        assert sessions.require_active("s-1", USER)["status"] == "ACTIVE"

    def test_a_session_that_was_never_started_is_refused(self, table):
        with pytest.raises(sessions.SessionEnded) as ended:
            sessions.require_active("never-started", USER)
        assert ended.value.reason is None

    def test_someone_elses_session_is_refused(self, table):
        sessions.start("s-1", USER, ROLES)
        with pytest.raises(sessions.SessionEnded):
            sessions.require_active("s-1", "someone.else")

    def test_signed_out_session_is_refused_with_the_reason(self, table):
        sessions.start("s-1", USER, ROLES)
        sessions.end("s-1", USER, sessions.SIGNED_OUT)
        with pytest.raises(sessions.SessionEnded) as ended:
            sessions.require_active("s-1", USER)
        assert ended.value.reason == sessions.SIGNED_OUT

    def test_fifteen_minutes_without_a_request_ends_the_session(self, table, clock):
        sessions.start("s-1", USER, ROLES)
        clock.advance(15 * 60)
        assert sessions.require_active("s-1", USER)            # exactly at the limit: still open
        clock.advance(15 * 60 + 1)
        with pytest.raises(sessions.SessionEnded) as ended:
            sessions.require_active("s-1", USER)
        assert ended.value.reason == sessions.IDLE
        assert stored(table)["status"] == "ENDED" and stored(table)["end_reason"] == "IDLE"

    def test_activity_keeps_the_session_open(self, table, clock):
        sessions.start("s-1", USER, ROLES)
        for _ in range(8):                                       # two hours of a request every 15 minutes
            clock.advance(15 * 60)
            sessions.require_active("s-1", USER)
        assert stored(table)["status"] == "ACTIVE"

    def test_the_absolute_limit_ends_even_an_active_session(self, table, clock):
        sessions.start("s-1", USER, ROLES)
        for _ in range(31):                                      # 7 hours 45 minutes of steady activity
            clock.advance(15 * 60)
            sessions.require_active("s-1", USER)
        clock.advance(15 * 60)                                   # eight hours after signing in
        with pytest.raises(sessions.SessionEnded) as ended:
            sessions.require_active("s-1", USER)
        assert ended.value.reason == sessions.EXPIRED

    def test_last_seen_is_written_at_most_once_a_minute(self, table, clock):
        sessions.start("s-1", USER, ROLES)
        first = stored(table)["last_seen_epoch"]
        clock.advance(30)
        sessions.require_active("s-1", USER)
        assert stored(table)["last_seen_epoch"] == first
        clock.advance(30)
        sessions.require_active("s-1", USER)
        assert stored(table)["last_seen_epoch"] == first + 60

    def test_the_idle_limit_is_a_setting(self, table, clock, monkeypatch):
        monkeypatch.setenv("FINTRUST_SESSION_IDLE_MINUTES", "5")
        sessions.start("s-1", USER, ROLES)
        clock.advance(5 * 60 + 1)
        with pytest.raises(sessions.SessionEnded):
            sessions.require_active("s-1", USER)


class TestEnd:
    def test_ending_twice_changes_nothing(self, table):
        sessions.start("s-1", USER, ROLES)
        assert sessions.end("s-1", USER, sessions.SIGNED_OUT) is True
        assert sessions.end("s-1", USER, sessions.REVOKED) is False
        assert stored(table)["end_reason"] == "SIGNED_OUT"

    def test_nobody_can_end_someone_elses_session(self, table):
        sessions.start("s-1", USER, ROLES)
        assert sessions.end("s-1", "someone.else", sessions.SIGNED_OUT) is False
        assert stored(table)["status"] == "ACTIVE"

    def test_unknown_reason_is_a_programming_error(self, table):
        with pytest.raises(ValueError):
            sessions.end("s-1", USER, "BORED")

    def test_an_administrator_can_end_every_session_of_one_person(self, table):
        sessions.start("laptop", USER, ROLES)
        sessions.start("phone", USER, ROLES)
        sessions.start("other", "l.dube", ("compliance-analyst",))
        assert sessions.end_all_for_user(USER) == 2
        assert stored(table, "laptop")["end_reason"] == "REVOKED"
        assert stored(table, "phone")["status"] == "ENDED"
        assert stored(table, "other")["status"] == "ACTIVE"


class TestPublicView:
    def test_only_what_the_dashboard_needs(self, table, clock):
        item, _ = sessions.start("s-1", USER, ROLES, "Thabo Nkosi")
        view = sessions.public_view(item)
        assert view == {
            "username": USER, "name": "Thabo Nkosi", "roles": ["compliance-officer"], "status": "ACTIVE",
            "started_at": "2026-10-03T04:00:00Z", "last_seen_at": "2026-10-03T04:00:00Z",
            "ends_at": "2026-10-03T12:00:00Z", "idle_minutes": 15,
        }


class TestFailures:
    def test_a_missing_table_is_a_session_error_not_a_crash(self, aws, monkeypatch):
        monkeypatch.setenv("FINTRUST_SSM_ENABLED", "false")
        monkeypatch.setenv("FINTRUST_SESSIONS_TABLE", "no-such-table")
        sessions.reset()
        with pytest.raises(sessions.SessionError):
            sessions.require_active("s-1", USER)
        sessions.reset()
