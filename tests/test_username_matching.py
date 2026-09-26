"""Username matching tolerates a leading ``@`` in config.

Telegram (and Slack/Discord) report bare usernames (``alice``), while the
docs and ``.env.example`` show ``@alice`` in ``ALLOW_FROM`` / ``USER_ROLES``.
Both spellings must match, or a documented config silently locks users out.
"""

from __future__ import annotations

from langclaw.gateway.utils import is_allowed, lookup_by_user


class TestIsAllowedUsername:
    def test_at_prefixed_config_matches_bare_username(self) -> None:
        assert is_allowed(["@alice"], "111", "alice")

    def test_bare_config_matches_bare_username(self) -> None:
        assert is_allowed(["alice"], "111", "alice")

    def test_bare_config_matches_at_prefixed_username(self) -> None:
        assert is_allowed(["alice"], "111", "@alice")

    def test_other_username_rejected(self) -> None:
        assert not is_allowed(["@alice"], "111", "mallory")

    def test_user_id_still_matches(self) -> None:
        assert is_allowed(["111"], "111", None)

    def test_empty_allow_list_allows_everyone(self) -> None:
        assert is_allowed([], "111", "anyone")

    def test_matrix_style_ids_unaffected(self) -> None:
        assert is_allowed(["@alice:matrix.org"], "@alice:matrix.org")
        assert not is_allowed(["@alice:matrix.org"], "@bob:matrix.org")


class TestLookupByUser:
    def test_user_id_wins(self) -> None:
        roles = {"111": "admin", "@alice": "viewer"}
        assert lookup_by_user(roles, "111", "alice") == "admin"

    def test_at_prefixed_key_matches_bare_username(self) -> None:
        assert lookup_by_user({"@alice": "editor"}, "111", "alice") == "editor"

    def test_bare_key_matches_bare_username(self) -> None:
        assert lookup_by_user({"alice": "editor"}, "111", "alice") == "editor"

    def test_no_match_returns_none(self) -> None:
        assert lookup_by_user({"@alice": "editor"}, "111", "bob") is None
        assert lookup_by_user({"@alice": "editor"}, "111", None) is None
