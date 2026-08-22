from unittest.mock import Mock

import pytest

from capybot.webui import apply_api


def test_begin_login_immediately_refreshes_status_cache(monkeypatch) -> None:
    status = {
        "logged_in": True,
        "cdp_alive": True,
        "chat_page_open": True,
        "profile_ready": True,
    }
    monkeypatch.setattr(apply_api._boss, "begin_login", Mock(return_value=dict(status)))
    apply_api._boss_status_cache.update(
        {"value": None, "checked_at": 0.0, "checked_monotonic": 0.0}
    )

    result = apply_api.apply_begin_login()
    cached = apply_api.apply_login_status()

    assert result["logged_in"] is True
    assert cached["logged_in"] is True
    assert cached["cached"] is True


def test_import_rejects_when_dedicated_browser_is_not_running(monkeypatch) -> None:
    monkeypatch.setattr(
        apply_api,
        "apply_login_status",
        Mock(return_value={"logged_in": False, "profile_ready": True, "cdp_alive": False}),
    )

    with pytest.raises(apply_api.ApplyAPIError, match="专用 BOSS 浏览器") as exc_info:
        apply_api.apply_import_start(30)

    assert exc_info.value.status == 409


def test_boss_status_uses_persisted_account_for_same_profile(monkeypatch) -> None:
    apply_api._boss_status_cache.update(
        {"value": None, "checked_at": 0.0, "checked_monotonic": 0.0}
    )
    monkeypatch.setattr(
        apply_api._boss,
        "login_status",
        Mock(
            return_value={
                "logged_in": True,
                "profile_dir": "profile-a",
                "account": {
                    "account_uid": "profile:placeholder",
                    "display_name": "BOSS 本地账号",
                    "profile_dir": "profile-a",
                },
            }
        ),
    )

    store = Mock()
    store.current_account.return_value = {
        "id": "account-1",
        "account_uid": "real-uid",
        "display_name": "真实账号",
        "profile_dir": "profile-a",
        "source": "browser_profile",
    }
    monkeypatch.setattr(apply_api, "_store", Mock(return_value=store))

    status = apply_api.apply_login_status(force=True)

    assert status["account"]["account_uid"] == "real-uid"
    assert status["account"]["display_name"] == "真实账号"
