import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from capybot.connectors.boss import (
    BOSS_CHAT_URL,
    BossConnector,
    BossConnectorError,
    RawCDPPage,
)


def test_boss_job_source_url_must_match_host_and_job_id() -> None:
    valid = "https://www.zhipin.com/job_detail/job-123.html"
    assert BossConnector._trusted_job_url(valid, "job-123") == valid
    assert BossConnector._trusted_job_url(valid, "other-job") == ""
    assert (
        BossConnector._trusted_job_url(
            "https://example.com/job_detail/job-123.html",
            "job-123",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_boss_job_source_url_retries_one_transient_page_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = BossConnector()
    detail = {"jobInfo": {"jobName": "AI Agent 实习生"}}
    reader = AsyncMock(
        side_effect=[BossConnectorError("BOSS 只读页面加载超时"), detail]
    )
    monkeypatch.setattr(connector, "_read_rendered_job_page_async", reader)

    result = await connector.fetch_job_detail_for_opportunity_async(
        platform_job_id="job-123",
        title="AI Agent 实习生",
        company="示例公司",
        source_url="https://www.zhipin.com/job_detail/job-123.html",
    )

    assert result == detail
    assert reader.await_count == 2


class _DisconnectedWebSocket:
    async def send(self, _payload: str) -> None:
        raise ConnectionError("no close frame received or sent")


class _FlakyPage(RawCDPPage):
    def __init__(self) -> None:
        super().__init__("ws://test")
        self._ws = object()

    async def current_url(self) -> str:
        raise BossConnectorError("no close frame received or sent")


class _ChatPage(RawCDPPage):
    def __init__(self) -> None:
        super().__init__("ws://test")
        self._ws = object()

    async def current_url(self) -> str:
        return BOSS_CHAT_URL


@pytest.mark.asyncio
async def test_raw_cdp_connection_error_is_normalized() -> None:
    page = RawCDPPage("ws://test")
    page._ws = _DisconnectedWebSocket()

    with pytest.raises(BossConnectorError, match="CDP connection closed"):
        await page.evaluate("location.href")
    assert page.is_closed()


@pytest.mark.asyncio
async def test_chat_page_reconnects_once_after_target_disconnect(tmp_path: Path) -> None:
    boss = BossConnector(tmp_path / "profile")
    recovered = _ChatPage()
    boss._async_page = _FlakyPage()
    boss._discard_async_page = AsyncMock()
    boss._ensure_async_page = AsyncMock(return_value=recovered)

    page = await boss._ensure_chat_page_async(boss._async_page)

    assert page is recovered
    boss._discard_async_page.assert_awaited_once()
    boss._ensure_async_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_30_day_conversation_state_is_a_success(tmp_path: Path) -> None:
    boss = BossConnector(tmp_path / "profile")
    page = AsyncMock()
    page.evaluate.return_value = {
        "url": BOSS_CHAT_URL,
        "title": "BOSS直聘",
        "bodyText": "全部 未读 新招呼 30天内暂无联系人 当前暂无消息",
        "loginLike": False,
    }
    boss._visible_conversation_rows_async = AsyncMock(return_value=[])
    boss._scroll_conversation_list_async = AsyncMock(return_value=False)

    assert await boss._conversation_rows_async(page) == []


def test_conversation_scan_status_distinguishes_explicit_empty(tmp_path: Path) -> None:
    boss = BossConnector(tmp_path / "profile")

    boss._remember_conversation_scan([], explicitly_empty=True)

    assert boss.conversation_scan_status() == {
        "readable": True,
        "explicitly_empty": True,
        "conversation_count": 0,
        "message": "BOSS 明确显示近 30 天暂无联系人，本地历史数据保持不变。",
    }


def test_conversation_match_prefers_company_and_hr_name(tmp_path: Path) -> None:
    boss = BossConnector(tmp_path / "profile")

    matched = boss._conversation_match_score(
        "05月20日 余女士 华智AI 招聘经理 计划实习多久？",
        company="华智AI",
        contact_names=["余女士"],
        title="AI应用开发实习生",
    )
    unrelated = boss._conversation_match_score(
        "李先生 另一家公司 Java 开发",
        company="华智AI",
        contact_names=["余女士"],
        title="AI应用开发实习生",
    )

    assert matched == 11
    assert unrelated == 0


@pytest.mark.asyncio
async def test_resolve_conversation_clicks_only_best_matching_row(tmp_path: Path) -> None:
    boss = BossConnector(tmp_path / "profile")
    page = AsyncMock()
    boss._ensure_async_page = AsyncMock(return_value=page)
    boss._ensure_chat_page_async = AsyncMock(return_value=page)
    boss._visible_conversation_rows_async = AsyncMock(
        return_value=[
            {"index": 0, "text": "李先生 另一家公司 Java 开发"},
            {"index": 7, "text": "余女士 华智AI 计划实习多久？"},
        ]
    )
    boss._conversation_identity_for_index_async = AsyncMock(
        return_value={"boss_uid": "fresh-boss-id"}
    )

    result = await boss.resolve_conversation_async(
        company="华智AI",
        contact_names=["余女士"],
        title="AI应用开发实习生",
    )

    assert result["boss_uid"] == "fresh-boss-id"
    assert result["match_score"] == 11
    boss._conversation_identity_for_index_async.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_resolve_conversation_reports_outside_current_window(
    tmp_path: Path,
) -> None:
    boss = BossConnector(tmp_path / "profile")
    page = AsyncMock()
    page.evaluate.return_value = {
        "url": BOSS_CHAT_URL,
        "bodyText": "消息 30天内暂无联系人 当前暂无消息",
    }
    boss._ensure_async_page = AsyncMock(return_value=page)
    boss._ensure_chat_page_async = AsyncMock(return_value=page)
    boss._visible_conversation_rows_async = AsyncMock(return_value=[])
    boss._scroll_conversation_list_async = AsyncMock(return_value=False)

    with pytest.raises(BossConnectorError, match="当前 30 天会话窗口"):
        await boss.resolve_conversation_async(company="示例科技")


@pytest.mark.asyncio
async def test_list_conversations_reuses_vue_identity_without_clicking(
    tmp_path: Path,
) -> None:
    boss = BossConnector(tmp_path / "profile")
    page = AsyncMock()
    boss._ensure_async_page = AsyncMock(return_value=page)
    boss._ensure_chat_page_async = AsyncMock(return_value=page)
    boss._conversation_rows_async = AsyncMock(
        return_value=[
            {
                "index": 0,
                "text": "王女士 示例科技 Agent 开发实习生",
                "boss_uid": "encrypted-boss-1",
                "security_id": "conversation-security-1",
                "group_id": "",
                "friend_source": 0,
            }
        ]
    )
    boss._conversation_identity_for_index_async = AsyncMock()

    rows = await boss.list_conversations_async()

    assert rows[0]["boss_uid"] == "encrypted-boss-1"
    assert rows[0]["security_id"] == "conversation-security-1"
    boss._conversation_identity_for_index_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_request_carries_current_conversation_security_context(
    tmp_path: Path,
) -> None:
    boss = BossConnector(tmp_path / "profile")
    page = AsyncMock()
    page.evaluate.return_value = {
        "code": 0,
        "zpData": {"messages": [], "hasMore": False},
    }
    boss._ensure_async_page = AsyncMock(return_value=page)
    boss._ensure_chat_page_async = AsyncMock(return_value=page)

    await boss.fetch_messages_async(
        "encrypted-boss-1",
        security_id="conversation-security-1",
        group_id="group-1",
        friend_source=2,
    )

    arguments = page.evaluate.await_args.args[1]
    assert arguments == {
        "bossId": "encrypted-boss-1",
        "securityId": "conversation-security-1",
        "groupId": "group-1",
        "friendSource": 2,
        "maxMsgId": 0,
    }


def test_job_candidate_requires_same_title_and_company(tmp_path: Path) -> None:
    boss = BossConnector(tmp_path / "profile")
    candidates = [
        {
            "title": "Agent 开发实习生",
            "company": "另一家公司",
            "href": "https://www.zhipin.com/job_detail/wrong.html",
        },
        {
            "title": "Agent开发实习生",
            "company": "示例科技有限公司",
            "href": "https://www.zhipin.com/job_detail/right.html",
        },
    ]

    selected = boss._select_job_candidate(
        candidates,
        title="Agent 开发实习生",
        company="示例科技有限公司",
    )

    assert selected is not None
    assert selected["href"].endswith("/right.html")


def test_external_chrome_starts_on_chat_and_detaches_on_windows(
    tmp_path: Path, monkeypatch
) -> None:
    boss = BossConnector(tmp_path / "profile")
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(boss, "_is_cdp_alive", lambda _port: False)
    monkeypatch.setattr(subprocess, "Popen", popen)

    boss._start_external_chrome(Path("chrome.exe"), 9222)

    args, kwargs = popen.call_args
    assert BOSS_CHAT_URL in args[0]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.DETACHED_PROCESS
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP


def test_begin_login_reuses_external_chrome_profile(tmp_path: Path, monkeypatch) -> None:
    boss = BossConnector(tmp_path / "profile")
    boss._debug_port = 9222
    executable = Path("chrome.exe")
    start = Mock()
    monkeypatch.setattr(boss, "_browser_executable", lambda: executable)
    monkeypatch.setattr(boss, "_start_external_chrome", start)
    monkeypatch.setattr(boss, "_cdp_websocket_url", Mock(return_value="ws://test"))
    open_tab = Mock()
    monkeypatch.setattr(boss, "_open_or_activate_cdp_url", open_tab)
    monkeypatch.setattr(
        boss,
        "login_status",
        lambda: {"logged_in": False, "cdp_alive": True},
    )
    monkeypatch.setattr(
        boss,
        "_ensure_page",
        Mock(side_effect=AssertionError("Playwright must not claim an active profile")),
    )

    status = boss.begin_login()

    start.assert_called_once_with(executable, 9222)
    open_tab.assert_called_once_with(9222, BOSS_CHAT_URL)
    assert status["browser"] == "system_chrome"
    assert status["cdp_port"] == 9222


def test_begin_login_waits_for_chat_redirect_to_settle(tmp_path: Path, monkeypatch) -> None:
    boss = BossConnector(tmp_path / "profile")
    boss._debug_port = 9222
    monkeypatch.setattr(boss, "_browser_executable", lambda: Path("chrome.exe"))
    monkeypatch.setattr(boss, "_start_external_chrome", Mock())
    monkeypatch.setattr(boss, "_cdp_websocket_url", Mock(return_value="ws://test"))
    monkeypatch.setattr(boss, "_open_or_activate_cdp_url", Mock())
    statuses = iter(
        [
            {"logged_in": True, "current_url": BOSS_CHAT_URL},
            {"logged_in": False, "current_url": "https://www.zhipin.com/web/user/"},
        ]
    )
    monkeypatch.setattr(boss, "login_status", lambda: next(statuses))
    monkeypatch.setattr("capybot.connectors.boss.time.sleep", Mock())

    status = boss.begin_login()

    assert status["logged_in"] is False
    assert status["current_url"].endswith("/web/user/")


def test_stable_login_status_waits_for_profile_restore(tmp_path: Path, monkeypatch) -> None:
    boss = BossConnector(tmp_path / "profile")
    statuses = iter(
        [
            {"logged_in": False, "cdp_alive": True, "current_url": "about:blank"},
            {"logged_in": True, "cdp_alive": True, "current_url": BOSS_CHAT_URL},
            {"logged_in": True, "cdp_alive": True, "current_url": BOSS_CHAT_URL},
        ]
    )
    monkeypatch.setattr(boss, "login_status", lambda: next(statuses))
    monkeypatch.setattr("capybot.connectors.boss.time.sleep", Mock())

    status = boss._stable_login_status()

    assert status["logged_in"] is True
    assert status["current_url"] == BOSS_CHAT_URL
