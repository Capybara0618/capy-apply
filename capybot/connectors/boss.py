"""Read-only BOSS browser connector used by import and the external MCP server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from capybot.runtime import ensure_dir, runtime_dir

from .cdp import CDPError, RawCDPPage

BOSS_HOME_URL = "https://www.zhipin.com/"
BOSS_CHAT_URL = "https://www.zhipin.com/web/geek/chat"
BOSS_JOBS_URL = "https://www.zhipin.com/web/geek/jobs"
BOSS_JOB_DETAIL_API = "/wapi/zpgeek/job/detail.json"
BROWSER_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""


BossConnectorError = CDPError


class BossConnector:
    """Read-only tool surface used by SnapshotImporter.

    If ``CAPYBOT_BOSS_FIXTURE`` points to a JSON file, data is read from that
    fixture. Otherwise Capybot controls an isolated Chrome through CDP and
    reads the same read-only endpoints used by the visible BOSS web page.
    """

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        *,
        fixture_data: dict[str, Any] | None = None,
    ):
        self.profile_dir = (
            Path(profile_dir).expanduser()
            if profile_dir
            else runtime_dir("browser") / "boss-profile"
        )
        ensure_dir(self.profile_dir)
        self.debug_port_file = self.profile_dir / "capybot-cdp-port.txt"
        self._fixture = fixture_data if fixture_data is not None else self._load_fixture()
        self._playwright = None
        self._context = None
        self._page = None
        self._async_playwright = None
        self._async_browser = None
        self._async_context = None
        self._async_page = None
        self._chrome_process = None
        self._debug_port = self._read_debug_port()
        self._last_conversation_scan: dict[str, Any] = {
            "readable": False,
            "explicitly_empty": False,
            "conversation_count": None,
            "message": "尚未扫描 BOSS 会话列表。",
        }

    @property
    def tools(self) -> list[str]:
        return [
            "boss_login_status",
            "boss_begin_login",
            "boss_list_conversations",
            "boss_fetch_messages",
            "boss_fetch_job_cards",
            "boss_fetch_job_detail",
        ]

    @property
    def fixture_mode(self) -> bool:
        return self._fixture is not None

    def _load_fixture(self) -> dict[str, Any] | None:
        raw = os.environ.get("CAPYBOT_BOSS_FIXTURE")
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.exists():
            raise BossConnectorError(f"CAPYBOT_BOSS_FIXTURE not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def login_status(self) -> dict[str, Any]:
        if self._fixture is not None:
            return {
                "logged_in": True,
                "fixture": True,
                "profile_dir": str(self.profile_dir),
                "account": self.account_snapshot(),
            }
        profile_ready = self.profile_dir.exists() and any(self.profile_dir.iterdir())
        cdp_alive = bool(self._debug_port and self._is_cdp_alive(self._debug_port))
        current_url = ""
        chat_page_open = False
        if cdp_alive and self._debug_port:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(
                    f"http://127.0.0.1:{self._debug_port}/json/list", timeout=1
                ) as response:
                    pages = json.loads(response.read().decode("utf-8"))
                if isinstance(pages, list):
                    for page in pages:
                        url = str(page.get("url") or "")
                        if url and not current_url:
                            current_url = url
                        if "zhipin.com" in url and "zhipin.com" not in current_url:
                            current_url = url
                        if "zhipin.com/web/geek/chat" in url:
                            current_url = url
                            chat_page_open = True
                            break
            except Exception:
                pass
        return {
            "logged_in": chat_page_open,
            "fixture": False,
            "profile_ready": profile_ready,
            "cdp_alive": cdp_alive,
            "chat_page_open": chat_page_open,
            "current_url": current_url,
            "profile_dir": str(self.profile_dir),
            "account": self.account_snapshot(),
        }

    def account_snapshot(self) -> dict[str, Any]:
        if self._fixture is not None:
            account = self._fixture.get("account") or {}
            return {
                "id": str(account.get("id") or "boss_fixture"),
                "account_uid": str(account.get("account_uid") or account.get("uid") or "fixture"),
                "display_name": account.get("display_name")
                or account.get("name")
                or "BOSS 模拟账号",
                "profile_dir": str(self.profile_dir),
                "source": str(account.get("source") or "fixture"),
                "raw": account,
            }
        fingerprint = hashlib.sha1(str(self.profile_dir.resolve()).encode("utf-8")).hexdigest()[:12]
        return {
            "id": f"boss_profile_{fingerprint}",
            "account_uid": f"profile:{fingerprint}",
            "display_name": "BOSS 本地账号",
            "profile_dir": str(self.profile_dir),
            "source": "browser_profile",
        }

    async def account_snapshot_async(self) -> dict[str, Any]:
        base = self.account_snapshot()
        if self._fixture is not None:
            return base
        try:
            page = await self._ensure_async_page()
            extracted = await page.evaluate(
                """
                () => {
                  const selectors = [
                    '.nav-figure .label-text', '.nav-figure [ka="header-username"]',
                    '.user-name', '.username', '.geek-name', '.nav-name',
                    '.user-info .name'
                  ];
                  const name = selectors
                    .map((sel) => document.querySelector(sel)?.textContent?.trim())
                    .find(Boolean);
                  const identityRoots = [
                    window._PAGE,
                    window.__INITIAL_STATE__?.userInfo,
                    window.__INITIAL_STATE__?.user,
                    window.__INITIAL_STATE__?.geek,
                    window.__NEXT_DATA__?.props?.pageProps?.userInfo,
                  ].filter(Boolean);
                  for (let i = 0; i < localStorage.length; i += 1) {
                    const key = localStorage.key(i) || '';
                    if (!/(user|geek|login|identity)/i.test(key)) continue;
                    try {
                      const value = JSON.parse(localStorage.getItem(key) || 'null');
                      if (value && typeof value === 'object') identityRoots.push(value);
                    } catch (_) {}
                  }
                  const identity = identityRoots.find((item) =>
                    item && (item.geekId || item.geekUid || item.userId || item.uid)
                  ) || {};
                  const identityEl = document.querySelector('[data-geek-id], [data-geek-uid], .user-info[data-uid]');
                  const accountUid = identity.geekId || identity.geekUid || identity.userId || identity.uid
                    || identityEl?.getAttribute('data-geek-id')
                    || identityEl?.getAttribute('data-geek-uid')
                    || identityEl?.getAttribute('data-uid')
                    || '';
                  const title = document.title || '';
                  return { account_uid: String(accountUid || ''), display_name: name || '', page_title: title, url: location.href };
                }
                """
            )
            if isinstance(extracted, dict):
                display_name = str(extracted.get("display_name") or "").strip()
                if display_name and len(display_name) <= 40:
                    base["display_name"] = display_name
                account_uid = str(extracted.get("account_uid") or "").strip()
                if account_uid and len(account_uid) <= 128:
                    base["account_uid"] = account_uid
                    identity_hash = hashlib.sha1(f"boss|{account_uid}".encode("utf-8")).hexdigest()[
                        :16
                    ]
                    base["id"] = f"boss_account_{identity_hash}"
                base["raw"] = extracted
        except Exception:
            pass
        return base

    def conversation_scan_status(self) -> dict[str, Any]:
        """Describe the latest list read without exposing page content."""

        return dict(self._last_conversation_scan)

    def begin_login(self) -> dict[str, Any]:
        if self._fixture is not None:
            return self.login_status()
        executable = self._browser_executable()
        if executable is not None:
            self._debug_port = self._debug_port or self._free_port()
            self._start_external_chrome(executable, self._debug_port)
            self._cdp_websocket_url(self._debug_port, timeout_s=10)
            self._open_or_activate_cdp_url(self._debug_port, BOSS_CHAT_URL)
            return {
                **self._stable_login_status(),
                "browser": "system_chrome",
                "cdp_port": self._debug_port,
                "login_url": BOSS_CHAT_URL,
            }
        page = self._ensure_page()
        page.goto(BOSS_HOME_URL, wait_until="domcontentloaded")
        return self.login_status()

    async def begin_login_async(self) -> dict[str, Any]:
        if self._fixture is not None:
            return self.login_status()
        executable = self._browser_executable()
        if executable is not None:
            self._debug_port = self._debug_port or self._free_port()
            self._start_external_chrome(executable, self._debug_port)
            await asyncio.to_thread(self._cdp_websocket_url, self._debug_port, 10)
            self._open_or_activate_cdp_url(self._debug_port, BOSS_CHAT_URL)
            return {
                **(await self._stable_login_status_async()),
                "browser": "system_chrome",
                "cdp_port": self._debug_port,
                "login_url": BOSS_CHAT_URL,
            }
        for attempt in range(2):
            page = await self._ensure_async_page()
            try:
                await page.goto(BOSS_HOME_URL, wait_until="domcontentloaded")
                break
            except Exception:
                if attempt:
                    raise
                await self.close_async()
        return self.login_status()

    def _stable_login_status(self, wait_s: float = 5.0) -> dict[str, Any]:
        """Wait for profile restoration or a login redirect to settle."""
        interval = 0.5
        status = self.login_status()
        for _ in range(max(1, int(wait_s / interval))):
            if status.get("logged_in"):
                time.sleep(interval)
                return self.login_status()
            time.sleep(interval)
            status = self.login_status()
        return status

    async def _stable_login_status_async(self, wait_s: float = 5.0) -> dict[str, Any]:
        interval = 0.5
        status = self.login_status()
        for _ in range(max(1, int(wait_s / interval))):
            if status.get("logged_in"):
                await asyncio.sleep(interval)
                return self.login_status()
            await asyncio.sleep(interval)
            status = self.login_status()
        return status

    def list_conversations(self, days: int = 30, limit: int = 200) -> list[dict[str, Any]]:
        if self._fixture is not None:
            rows = list(self._fixture.get("conversations", []))[:limit]
            self._remember_conversation_scan(rows, explicitly_empty=not rows)
            return rows
        page = self._ensure_page()
        page.goto(BOSS_CHAT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        rows = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('.friend-content')).slice(0, 200).map((el, i) => {
              const text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
              const unreadEl = el.querySelector('[class*="badge"], [class*="unread"], [class*="count"]');
              return { index: i, text, unread: unreadEl ? parseInt(unreadEl.textContent || '0') || 0 : 0 };
            })
            """
        )
        if not rows:
            state = page.evaluate(
                """
                () => ({
                  url: location.href,
                  bodyText: (document.body && document.body.innerText || '').slice(0, 500)
                })
                """
            )
            body_text = str(state.get("bodyText") or "")
            if "暂无联系人" not in body_text and "暂无会话" not in body_text:
                raise BossConnectorError(
                    "未读取到 BOSS 会话列表，也没有检测到明确的空列表状态。"
                    f" 当前页面：{state.get('url') or 'unknown'}"
                )
        out: list[dict[str, Any]] = []
        for row in rows[:limit]:
            boss_id = self._boss_id_for_index(int(row["index"]))
            if not boss_id:
                out.append(
                    {
                        "id": f"boss_dom_{row['index']}",
                        "boss_uid": "",
                        "contact_name": self._first_token(row.get("text", "")),
                        "last_message_preview": row.get("text", "")[:120],
                        "raw_payload": row,
                    }
                )
                continue
            out.append(
                {
                    "id": f"boss_{boss_id}",
                    "conversation_id": f"boss_{boss_id}",
                    "boss_uid": boss_id,
                    "contact_name": self._first_token(row.get("text", "")),
                    "last_message_preview": row.get("text", "")[:120],
                    "raw_payload": row,
                }
            )
        self._remember_conversation_scan(out, explicitly_empty=not out)
        return out

    async def list_conversations_async(
        self, days: int = 30, limit: int = 200
    ) -> list[dict[str, Any]]:
        if self._fixture is not None:
            rows = list(self._fixture.get("conversations", []))[:limit]
            self._remember_conversation_scan(rows, explicitly_empty=not rows)
            return rows
        rows: list[dict[str, Any]] = []
        for attempt in range(2):
            page = await self._ensure_async_page()
            try:
                page = await self._ensure_chat_page_async(page)
                rows = await self._conversation_rows_async(page, limit=limit)
                break
            except Exception as exc:
                if attempt or not self._is_transient_cdp_error(exc):
                    raise
                await self._discard_async_page()
        out: list[dict[str, Any]] = []
        for row in rows[:limit]:
            identity = self._conversation_identity_from_row(row)
            if not identity.get("boss_uid"):
                identity = await self._conversation_identity_for_index_async(int(row["index"]))
            boss_id = identity.get("boss_uid")
            if not boss_id:
                out.append(
                    {
                        "id": f"boss_dom_{row['index']}",
                        "boss_uid": "",
                        "contact_name": self._first_token(row.get("text", "")),
                        "last_message_preview": row.get("text", "")[:120],
                        "raw_payload": {**row, **identity},
                    }
                )
                continue
            out.append(
                {
                    "id": f"boss_{boss_id}",
                    "conversation_id": f"boss_{boss_id}",
                    "boss_uid": boss_id,
                    "contact_name": self._first_token(row.get("text", "")),
                    "last_message_preview": row.get("text", "")[:120],
                    "security_id": identity.get("security_id"),
                    "group_id": identity.get("group_id"),
                    "friend_source": identity.get("friend_source", 0),
                    "raw_payload": {**row, **identity},
                }
            )
        self._remember_conversation_scan(out, explicitly_empty=not out)
        return out

    async def resolve_conversation_async(
        self,
        *,
        company: str | None = None,
        contact_names: list[str] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Locate one current conversation without resolving every list item."""

        page = await self._ensure_async_page()
        page = await self._ensure_chat_page_async(page)
        candidates: list[tuple[int, dict[str, Any]]] = []
        seen: set[str] = set()
        for _ in range(20):
            rows = await self._visible_conversation_rows_async(page)
            for row in rows:
                text = str(row.get("text") or "")
                if not text or text in seen:
                    continue
                seen.add(text)
                score = self._conversation_match_score(
                    text,
                    company=company,
                    contact_names=contact_names or [],
                    title=title,
                )
                if score:
                    candidates.append((score, row))
            if candidates and max(score for score, _ in candidates) >= 5:
                break
            if not await self._scroll_conversation_list_async(page):
                break
            await page.wait_for_timeout(600)

        if not candidates:
            state = await page.evaluate(
                """
                () => ({
                  url: location.href,
                  bodyText: (document.body && document.body.innerText || '').slice(0, 1200)
                })
                """
            )
            body_text = str(state.get("bodyText") or "")
            if "30天内暂无联系人" in body_text:
                raise BossConnectorError("目标会话不在 BOSS 当前 30 天会话窗口中，无法实时刷新")
            raise BossConnectorError("当前 BOSS 聊天列表中未定位到该机会对应的会话")
        candidates.sort(key=lambda item: item[0], reverse=True)
        best_score = candidates[0][0]
        best = [row for score, row in candidates if score == best_score]
        if len(best) > 1:
            raise BossConnectorError("当前 BOSS 聊天列表中存在多个同分会话，无法安全定位")
        row = best[0]
        identity = self._conversation_identity_from_row(row)
        if not identity.get("boss_uid"):
            identity = await self._conversation_identity_for_index_async(int(row["index"]))
        boss_uid = identity.get("boss_uid")
        if not boss_uid:
            raise BossConnectorError("已定位目标会话，但未能读取当前 bossId")
        return {
            "boss_uid": boss_uid,
            "contact_name": self._first_token(str(row.get("text") or "")),
            "last_message_preview": str(row.get("text") or "")[:120],
            "match_score": best_score,
            "security_id": identity.get("security_id"),
            "group_id": identity.get("group_id"),
            "friend_source": identity.get("friend_source", 0),
            "raw_payload": {**row, **identity},
        }

    @staticmethod
    def _conversation_match_score(
        text: str,
        *,
        company: str | None,
        contact_names: list[str],
        title: str | None,
    ) -> int:
        haystack = "".join(text.casefold().split())
        score = 0
        company_key = "".join(str(company or "").casefold().split())
        if len(company_key) >= 2 and company_key in haystack:
            score += 6
        for name in contact_names:
            key = "".join(str(name or "").casefold().split())
            if len(key) >= 2 and key in haystack:
                score += 5
                break
        title_key = "".join(str(title or "").casefold().split())
        if len(title_key) >= 3 and title_key in haystack:
            score += 3
        return score

    def _remember_conversation_scan(
        self,
        rows: list[dict[str, Any]],
        *,
        explicitly_empty: bool,
    ) -> None:
        count = len(rows)
        self._last_conversation_scan = {
            "readable": True,
            "explicitly_empty": bool(explicitly_empty and count == 0),
            "conversation_count": count,
            "message": (
                "BOSS 明确显示近 30 天暂无联系人，本地历史数据保持不变。"
                if explicitly_empty and count == 0
                else f"BOSS 会话列表可读，共扫描到 {count} 个会话。"
            ),
        }

    async def _conversation_rows_async(self, page: Any, limit: int = 200) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(12):
            if await self._visible_conversation_rows_async(page):
                break
            await page.wait_for_timeout(1000)
        for _ in range(20):
            rows = await self._visible_conversation_rows_async(page)
            for row in rows:
                key = row.get("text") or str(row.get("index"))
                if key in seen:
                    continue
                seen.add(key)
                if not row.get("boss_uid"):
                    try:
                        row.update(
                            await self._conversation_identity_for_index_async(int(row["index"]))
                        )
                    except BossConnectorError:
                        if results:
                            return results
                        raise
                if not row.get("boss_uid") and "求职助手" in str(row.get("text") or ""):
                    continue
                results.append(row)
                if len(results) >= limit:
                    return results
                await page.wait_for_timeout(1200)
            if not await self._scroll_conversation_list_async(page):
                break
            await page.wait_for_timeout(1500)
        if results:
            return results

        state = await page.evaluate(
            """
            () => ({
              url: location.href,
              title: document.title,
              bodyText: (document.body && document.body.innerText || '').slice(0, 500),
              loginLike: !!document.querySelector('[class*=login], [class*=qrcode], [class*=scan], input[type=tel], input[type=password]')
            })
            """
        )
        body_text = str(state.get("bodyText") or "")
        if "暂无联系人" in body_text or "暂无会话" in body_text:
            return []
        raise BossConnectorError(
            "未读取到 BOSS 会话列表。请先在 Capybot 弹出的 Chrome 中完成 BOSS 登录，"
            "并确认 https://www.zhipin.com/web/geek/chat 能看到左侧聊天列表后再导入。"
            f" 当前页面：{state.get('url') or 'unknown'}"
        )

    async def _visible_conversation_rows_async(self, page: Any) -> list[dict[str, Any]]:
        return await page.evaluate(
            """
            () => {
              const readIdentity = (el) => {
                const candidates = [];
                let node = el;
                for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                  const vm = node.__vue__;
                  if (!vm) continue;
                  candidates.push(
                    vm.boss, vm.$props && vm.$props.boss, vm.$data && vm.$data.boss,
                    vm.item, vm.user, vm.friend, vm.conversation
                  );
                }
                const boss = candidates.find((item) => item && typeof item === 'object' && (
                  item.encryptBossId || item.securityId || item.uniqueId || item.friendId
                )) || {};
                return {
                  boss_uid: String(boss.encryptBossId || boss.encryptUserId || ''),
                  security_id: String(boss.securityId || ''),
                  group_id: String(boss.gid || boss.groupId || ''),
                  friend_source: Number(boss.friendSource || boss.source || 0),
                  unique_id: String(boss.uniqueId || ''),
                  current_job_id: String(boss.encryptJobId || boss.jobId || '')
                };
              };
              return Array.from(document.querySelectorAll('.friend-content')).slice(0, 200).map((el, i) => {
                const text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                const unreadEl = el.querySelector('[class*="badge"], [class*="unread"], [class*="count"]');
                return {
                  index: i,
                  text,
                  unread: unreadEl ? parseInt(unreadEl.textContent || '0') || 0 : 0,
                  ...readIdentity(el)
                };
              });
            }
            """
        )

    async def _scroll_conversation_list_async(self, page: Any) -> bool:
        return bool(
            await page.evaluate(
                """
            () => {
              const candidates = [
                document.querySelector('.user-list-content'),
                document.querySelector('.user-list'),
                document.querySelector('.chat-user.v2'),
                document.querySelector('.chat-user')
              ].filter(Boolean);
              const scroller = candidates.find(el => el.scrollHeight > el.clientHeight);
              if (!scroller) return false;
              const before = scroller.scrollTop;
              scroller.scrollTop = Math.min(scroller.scrollTop + Math.max(360, scroller.clientHeight * 0.8), scroller.scrollHeight);
              return scroller.scrollTop !== before;
            }
            """
            )
        )

    def fetch_messages(
        self,
        boss_uid: str,
        max_pages: int = 20,
        *,
        security_id: str = "",
        group_id: str = "",
        friend_source: int = 0,
    ) -> list[dict[str, Any]]:
        if self._fixture is not None:
            return list(self._fixture.get("messages", {}).get(boss_uid, []))
        if not boss_uid:
            raise BossConnectorError("missing boss_uid for historyMsg")
        page = self._ensure_page()
        all_messages: list[dict[str, Any]] = []
        max_msg_id = 0
        for _ in range(max_pages):
            data = page.evaluate(
                """
                async ({ bossId, securityId, groupId, friendSource, maxMsgId }) => {
                  const query = new URLSearchParams({
                    bossId,
                    securityId,
                    groupId,
                    gid: groupId,
                    maxMsgId: String(maxMsgId),
                    c: '20',
                    page: '1',
                    src: String(friendSource || 0)
                  });
                  const url = `/wapi/zpchat/geek/historyMsg?${query}`;
                  const response = await fetch(url);
                  return await response.json();
                }
                """,
                {
                    "bossId": boss_uid,
                    "securityId": security_id,
                    "groupId": group_id,
                    "friendSource": friend_source,
                    "maxMsgId": max_msg_id,
                },
            )
            if data.get("code") != 0 or not data.get("zpData"):
                raise BossConnectorError(data.get("msg") or "BOSS historyMsg API failed")
            messages = data["zpData"].get("messages") or []
            if not messages:
                break
            all_messages.extend(messages)
            if not data["zpData"].get("hasMore"):
                break
            max_msg_id = min(int(m.get("mid") or max_msg_id) for m in messages)
            page.wait_for_timeout(600)
        return all_messages

    async def fetch_messages_async(
        self,
        boss_uid: str,
        max_pages: int = 20,
        *,
        security_id: str = "",
        group_id: str = "",
        friend_source: int = 0,
    ) -> list[dict[str, Any]]:
        if self._fixture is not None:
            return list(self._fixture.get("messages", {}).get(boss_uid, []))
        if not boss_uid:
            raise BossConnectorError("missing boss_uid for historyMsg")
        page = await self._ensure_async_page()
        page = await self._ensure_chat_page_async(page)
        all_messages: list[dict[str, Any]] = []
        max_msg_id = 0
        for _ in range(max_pages):
            for attempt in range(2):
                try:
                    data = await page.evaluate(
                        """
                        async ({ bossId, securityId, groupId, friendSource, maxMsgId }) => {
                          const query = new URLSearchParams({
                            bossId,
                            securityId,
                            groupId,
                            gid: groupId,
                            maxMsgId: String(maxMsgId),
                            c: '20',
                            page: '1',
                            src: String(friendSource || 0)
                          });
                          const url = `/wapi/zpchat/geek/historyMsg?${query}`;
                          const response = await fetch(url);
                          return await response.json();
                        }
                        """,
                        {
                            "bossId": boss_uid,
                            "securityId": security_id,
                            "groupId": group_id,
                            "friendSource": friend_source,
                            "maxMsgId": max_msg_id,
                        },
                    )
                    break
                except Exception as exc:
                    if attempt or not self._is_transient_cdp_error(exc):
                        raise
                    await self._discard_async_page()
                    page = await self._ensure_async_page()
                    page = await self._ensure_chat_page_async(page)
            if data.get("code") != 0 or not data.get("zpData"):
                message = data.get("message") or data.get("msg") or "BOSS historyMsg API failed"
                if data.get("code") == 19 and not security_id:
                    message = f"{message}；当前会话缺少 securityId，请重新扫描会话列表"
                raise BossConnectorError(message)
            messages = data["zpData"].get("messages") or []
            if not messages:
                break
            all_messages.extend(messages)
            if not data["zpData"].get("hasMore"):
                break
            max_msg_id = min(int(m.get("mid") or max_msg_id) for m in messages)
            await page.wait_for_timeout(1200)
            page = await self._ensure_chat_page_async(page)
        return all_messages

    def fetch_job_cards(self, boss_uid: str) -> list[dict[str, Any]]:
        cards = []
        for message in self.fetch_messages(boss_uid):
            job = self._job_from_message(message)
            if job is not None:
                cards.append(job)
        return cards

    async def fetch_job_cards_async(self, boss_uid: str) -> list[dict[str, Any]]:
        cards = []
        for message in await self.fetch_messages_async(boss_uid):
            job = self._job_from_message(message)
            if job is not None:
                cards.append(job)
        return cards

    def fetch_job_detail(self, security_id: str) -> dict[str, Any]:
        """Read one complete job record through the signed-in BOSS page."""

        if self._fixture is not None:
            return self._fixture_job_detail(security_id)
        lookup_id = str(security_id or "").strip()
        if not lookup_id:
            raise BossConnectorError("missing security_id for BOSS job detail")
        page = self._ensure_page()
        data = page.evaluate(
            """
            async ({ securityId, endpoint }) => {
              const url = `${endpoint}?securityId=${encodeURIComponent(securityId)}`;
              const response = await fetch(url, { credentials: 'include' });
              return await response.json();
            }
            """,
            {"securityId": lookup_id, "endpoint": BOSS_JOB_DETAIL_API},
        )
        return self._unwrap_job_detail(data)

    async def fetch_job_detail_async(self, security_id: str) -> dict[str, Any]:
        """Async read-only job detail lookup used by the BOSS MCP server."""

        if self._fixture is not None:
            return self._fixture_job_detail(security_id)
        lookup_id = str(security_id or "").strip()
        if not lookup_id:
            raise BossConnectorError("missing security_id for BOSS job detail")
        page = await self._ensure_async_page()
        page = await self._ensure_chat_page_async(page)
        data = await page.evaluate(
            """
            async ({ securityId, endpoint }) => {
              const url = `${endpoint}?securityId=${encodeURIComponent(securityId)}`;
              const response = await fetch(url, { credentials: 'include' });
              return await response.json();
            }
            """,
            {"securityId": lookup_id, "endpoint": BOSS_JOB_DETAIL_API},
        )
        return self._unwrap_job_detail(data)

    async def fetch_job_detail_for_opportunity_async(
        self,
        *,
        platform_job_id: str,
        title: str,
        company: str,
        security_id: str = "",
        source_url: str = "",
    ) -> dict[str, Any]:
        """Read a job through BOSS's native search/detail pages.

        The chat-card ``securityId`` is short lived. Opening the current BOSS
        result page lets BOSS generate the current read context instead of
        replaying an expired API request.
        """

        if self._fixture is not None:
            return self._fixture_job_detail(platform_job_id or security_id)
        if not title and security_id:
            return await self.fetch_job_detail_async(security_id)

        trusted_source_url = self._trusted_job_url(source_url, platform_job_id)
        if trusted_source_url:
            last_error: BossConnectorError | None = None
            for attempt in range(2):
                try:
                    return await self._read_rendered_job_page_async(trusted_source_url)
                except BossConnectorError as exc:
                    last_error = exc
                    if attempt == 0:
                        await asyncio.sleep(0.8)
            if last_error is not None:
                raise last_error

        queries = [str(title or "").strip(), str(company or "").strip()]
        seen_queries: set[str] = set()
        for query in queries:
            if not query or query in seen_queries:
                continue
            seen_queries.add(query)
            candidates = await self._search_jobs_page_async(query)
            selected = self._select_job_candidate(
                candidates,
                title=title,
                company=company,
            )
            if not selected:
                continue
            return await self._read_rendered_job_page_async(str(selected["href"]))

        if security_id and not title:
            return await self.fetch_job_detail_async(security_id)
        raise BossConnectorError("BOSS 当前职位列表中未找到与该机会一致的岗位")

    @staticmethod
    def _trusted_job_url(source_url: str, platform_job_id: str) -> str:
        value = str(source_url or "").strip()
        expected_id = str(platform_job_id or "").strip()
        if not value or not expected_id:
            return ""
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in {
            "zhipin.com",
            "www.zhipin.com",
        }:
            return ""
        match = re.search(r"/job_detail/([^./?]+)", parsed.path)
        if not match or match.group(1) != expected_id:
            return ""
        return value

    async def _search_jobs_page_async(self, query: str) -> list[dict[str, str]]:
        url = f"{BOSS_JOBS_URL}?query={urllib.parse.quote(query)}"
        page = await self._open_temporary_cdp_page_async(url)
        try:
            await self._wait_for_document_async(page, timeout_s=8.0)
            deadline = time.monotonic() + 8.0
            state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                state = await page.evaluate(
                    """
                    () => ({
                      url: location.href,
                      bodyText: (document.body && document.body.innerText || '').slice(0, 1200),
                      rows: Array.from(document.querySelectorAll('.job-card-wrap')).slice(0, 30).map((el) => ({
                        title: (el.querySelector('.job-name')?.textContent || '').trim(),
                        company: (
                          el.querySelector('.company-name')?.textContent
                          || el.querySelector('.job-card-footer .company-info')?.textContent
                          || el.querySelector('.company-info h3')?.textContent
                          || ''
                        ).trim(),
                        href: el.querySelector('a.job-name[href*="job_detail"]')?.href || '',
                        summary: (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 500)
                      }))
                    })
                    """
                )
                body_text = str(state.get("bodyText") or "")
                if (
                    state.get("rows")
                    or "没有找到相关职位" in body_text
                    or "您的环境存在异常" in body_text
                ):
                    break
                await page.wait_for_timeout(300)
            body_text = str(state.get("bodyText") or "")
            if "您的环境存在异常" in body_text:
                raise BossConnectorError("您的环境存在异常")
            return [
                row
                for row in state.get("rows") or []
                if isinstance(row, dict) and str(row.get("href") or "").startswith("https://")
            ]
        finally:
            await page.close_target()

    async def search_company_jobs_async(
        self,
        company: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, str]]:
        """Return current BOSS jobs that explicitly name the target company."""

        expected = self._normalize_match_text(company)
        if not expected:
            return []
        rows = await self._search_jobs_page_async(company)
        matched: list[dict[str, str]] = []
        for row in rows:
            company_text = self._normalize_match_text(row.get("company", ""))
            summary = self._normalize_match_text(row.get("summary", ""))
            if expected not in company_text and expected not in summary:
                continue
            matched.append({**row, "company": company})
            if len(matched) >= max(1, limit):
                break
        return matched

    async def _read_rendered_job_page_async(self, url: str) -> dict[str, Any]:
        page = await self._open_temporary_cdp_page_async(url)
        try:
            await self._wait_for_document_async(page, timeout_s=8.0)
            deadline = time.monotonic() + 8.0
            data: dict[str, Any] = {}
            while time.monotonic() < deadline:
                data = await page.evaluate(
                    """
                () => {
                  const text = (selector) =>
                    (document.querySelector(selector)?.textContent || '').trim().replace(/\\s+/g, ' ');
                  const bodyText = (document.body && document.body.innerText || '').trim();
                  const info = window._jobInfo || {};
                  const detailNode =
                    document.querySelector('.job-detail-section .job-sec-text')
                    || document.querySelector('.job-sec-text')
                    || document.querySelector('.job-detail-section');
                  let description = (detailNode?.innerText || '').trim();
                  if (!description && bodyText.includes('职位描述')) {
                    description = bodyText.split('职位描述', 2)[1]
                      .split(/认证资质|BOSS 安全提示|更多职位/, 1)[0]
                      .trim();
                  }
                  const labels = Array.from(document.querySelectorAll(
                    '.job-detail-section .job-tags span, .job-tags span, .job-keyword-list li'
                  )).map((el) => (el.textContent || '').trim()).filter(Boolean);
                  const bossLines = (
                    document.querySelector('.job-boss-info')?.innerText || ''
                  ).split(/\\n+/).map((value) => value.trim()).filter(Boolean);
                  return {
                    url: location.href,
                    title: text('.job-banner h1') || text('.job-title') || info.job_name || '',
                    salary: text('.job-banner .salary') || info.job_salary || '',
                    company: (text('.job-banner .brand-name') || info.company || '')
                      .replace(/^代招公司[：:]?\\s*/, ''),
                    city: text('.job-banner .text-city'),
                    experience: text('.job-banner .text-experiece')
                      || text('.job-banner .text-experience'),
                    education: text('.job-banner .text-degree'),
                    status: text('.job-status'),
                    description,
                    skills: Array.from(new Set(labels)),
                    boss_name: bossLines[0] || '',
                    boss_role: bossLines.slice(1).join(' · '),
                    encrypt_job_id: String(info.job_id || ''),
                    security_id: String(info.securityId || ''),
                    body_preview: bodyText.slice(0, 1200)
                  };
                }
                """
                )
                body_preview = str(data.get("body_preview") or "")
                if (
                    (data.get("title") and data.get("description"))
                    or "您的环境存在异常" in body_preview
                    or "职位已不存在" in body_preview
                ):
                    break
                await page.wait_for_timeout(300)
            if "您的环境存在异常" in str(data.get("body_preview") or ""):
                raise BossConnectorError("您的环境存在异常")
            if not data.get("title") or not data.get("description"):
                raise BossConnectorError("BOSS 岗位详情页面未渲染出完整职位信息")
            return {
                "jobInfo": {
                    "encryptJobId": data.get("encrypt_job_id"),
                    "jobName": data.get("title"),
                    "salaryDesc": data.get("salary"),
                    "cityName": data.get("city"),
                    "experienceName": data.get("experience"),
                    "degreeName": data.get("education"),
                    "jobLabels": data.get("skills") or [],
                    "postDescription": data.get("description"),
                    "status": data.get("status"),
                },
                "brandComInfo": {"brandName": data.get("company")},
                "bossInfo": {
                    "name": data.get("boss_name"),
                    "title": data.get("boss_role"),
                },
                "jobDetail": data.get("description"),
                "_capybot_source": "boss_rendered_job_page",
                "_capybot_source_url": data.get("url"),
                "_capybot_security_id": data.get("security_id"),
            }
        finally:
            await page.close_target()

    async def _open_temporary_cdp_page_async(self, url: str) -> RawCDPPage:
        executable = self._browser_executable()
        if executable is None:
            raise BossConnectorError("读取 BOSS 页面需要本机 Chrome 或 Edge")
        await self._ensure_async_page()
        if self._debug_port is None:
            raise BossConnectorError("BOSS Chrome CDP 尚未启动")
        target = await asyncio.to_thread(self._create_cdp_target, self._debug_port, url)
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise BossConnectorError("BOSS 临时只读页面没有可用的 CDP 地址")
        return await RawCDPPage(ws_url).connect()

    @staticmethod
    def _create_cdp_target(port: int, url: str) -> dict[str, Any]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        encoded_url = urllib.parse.quote(url, safe=":/?=&%")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{encoded_url}",
            method="PUT",
        )
        try:
            with opener.open(request, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise BossConnectorError(f"无法打开 BOSS 临时只读页面：{exc}") from exc
        if not isinstance(data, dict):
            raise BossConnectorError("Chrome 没有返回有效的临时页面")
        return data

    @staticmethod
    async def _wait_for_document_async(page: RawCDPPage, *, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = await page.evaluate(
                """
                () => ({
                  ready: document.readyState === 'complete',
                  url: location.href,
                  hasBody: !!(document.body && document.body.innerText.trim())
                })
                """
            )
            if (
                isinstance(state, dict)
                and state.get("ready")
                and str(state.get("url") or "").startswith(("http://", "https://"))
                and state.get("hasBody")
            ):
                await page.wait_for_timeout(600)
                return
            await page.wait_for_timeout(250)
        raise BossConnectorError("BOSS 只读页面加载超时")

    @staticmethod
    def _select_job_candidate(
        candidates: list[dict[str, str]],
        *,
        title: str,
        company: str,
    ) -> dict[str, str] | None:
        expected_title = BossConnector._normalize_match_text(title)
        expected_company = BossConnector._normalize_match_text(company)
        ranked: list[tuple[int, dict[str, str]]] = []
        for candidate in candidates:
            actual_title = BossConnector._normalize_match_text(candidate.get("title", ""))
            actual_company = BossConnector._normalize_match_text(candidate.get("company", ""))
            summary = BossConnector._normalize_match_text(candidate.get("summary", ""))
            score = 0
            if expected_title and actual_title == expected_title:
                score += 8
            elif expected_title and (
                expected_title in actual_title or actual_title in expected_title
            ):
                score += 5
            if expected_company and actual_company == expected_company:
                score += 8
            elif expected_company and (
                expected_company in actual_company or actual_company in expected_company
            ):
                score += 4
            elif expected_company and expected_company in summary:
                score += 4
            if score >= (8 if not expected_company else 12):
                ranked.append((score, candidate))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return None
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None
        return ranked[0][1]

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        return "".join(character.casefold() for character in str(value) if character.isalnum())

    def clear_login_state(self) -> None:
        self.close()
        if self.profile_dir.exists():
            shutil.rmtree(self.profile_dir)
        ensure_dir(self.profile_dir)

    async def clear_login_state_async(self) -> None:
        await self.close_async()
        if self.profile_dir.exists():
            shutil.rmtree(self.profile_dir)
        ensure_dir(self.profile_dir)

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._context = self._page = self._playwright = None

    async def close_async(self) -> None:
        if isinstance(self._async_page, RawCDPPage):
            try:
                await self._async_page.close()
            except Exception:
                pass
            self._async_page = None
        if self._async_context is not None:
            try:
                await self._async_context.close()
            except Exception:
                pass
        if self._async_browser is not None:
            try:
                await self._async_browser.close()
            except Exception:
                pass
        if self._async_playwright is not None:
            try:
                await self._async_playwright.stop()
            except Exception:
                pass
        self._async_browser = self._async_context = self._async_page = self._async_playwright = None
        self._chrome_process = None

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BossConnectorError(
                "Playwright is required. Run `playwright install chromium`."
            ) from exc
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            **self._launch_options(),
        )
        self._context.add_init_script(BROWSER_INIT_SCRIPT)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    async def _ensure_async_page(self):
        if self._async_page is not None:
            try:
                if not self._async_page.is_closed():
                    return self._async_page
            except Exception:
                pass
            await self.close_async()
        executable = self._browser_executable()
        if executable is not None:
            return await self._ensure_async_cdp_page(executable)
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise BossConnectorError(
                "Playwright is required. Run `playwright install chromium`."
            ) from exc
        self._async_playwright = await async_playwright().start()
        self._async_context = await self._async_playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            **self._launch_options(),
        )
        await self._async_context.add_init_script(BROWSER_INIT_SCRIPT)
        self._async_page = (
            self._async_context.pages[0]
            if self._async_context.pages
            else await self._async_context.new_page()
        )
        return self._async_page

    async def _ensure_async_cdp_page(self, executable: Path):
        ws_url = None
        if self._debug_port is not None:
            try:
                ws_url = self._cdp_page_websocket_url(self._debug_port, timeout_s=2)
            except Exception:
                self._debug_port = None
        if self._debug_port is None:
            self._debug_port = self._free_port()
            self._write_debug_port(self._debug_port)
            self._start_external_chrome(executable, self._debug_port)
            ws_url = self._cdp_page_websocket_url(self._debug_port)
        elif ws_url is None:
            self._start_external_chrome(executable, self._debug_port)
            ws_url = self._cdp_page_websocket_url(self._debug_port)
        self._async_page = await RawCDPPage(ws_url).connect()
        return self._async_page

    async def _discard_async_page(self) -> None:
        if isinstance(self._async_page, RawCDPPage):
            await self._async_page.close()
        self._async_page = None

    @staticmethod
    def _is_transient_cdp_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "cdp connection closed",
                "no close frame",
                "connection closed",
                "connection reset",
                "target closed",
                "page.navigate",
                "websocket",
            )
        )

    def _start_external_chrome(self, executable: Path, port: int) -> None:
        if self._is_cdp_alive(port):
            return
        if self._chrome_process is not None and self._chrome_process.poll() is None:
            return
        ensure_dir(self.profile_dir)
        args = [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
            "--new-window",
            "--start-maximized",
            BOSS_CHAT_URL,
        ]
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        self._chrome_process = subprocess.Popen(
            args,
            **popen_kwargs,
        )
        self._write_debug_port(port)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _cdp_websocket_url(port: int, timeout_s: float = 45) -> str:
        url = f"http://127.0.0.1:{port}/json/version"
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            try:
                with opener.open(url, timeout=1) as response:
                    if response.status == 200:
                        data = json.loads(response.read().decode("utf-8"))
                        ws_url = data.get("webSocketDebuggerUrl")
                        if ws_url:
                            return str(ws_url)
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        raise BossConnectorError(f"Chrome CDP endpoint did not start: {last_error}")

    @staticmethod
    def _cdp_page_websocket_url(port: int, timeout_s: float = 45) -> str:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            try:
                with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                    if response.status != 200:
                        time.sleep(0.25)
                        continue
                    pages = json.loads(response.read().decode("utf-8"))
                    if not isinstance(pages, list):
                        time.sleep(0.25)
                        continue
                    chat_pages = [
                        page
                        for page in pages
                        if page.get("type") == "page"
                        and "/web/geek/chat" in str(page.get("url") or "")
                        and page.get("webSocketDebuggerUrl")
                    ]
                    zhipin_pages = [
                        page
                        for page in pages
                        if page.get("type") == "page"
                        and "zhipin.com" in str(page.get("url") or "")
                        and page.get("webSocketDebuggerUrl")
                    ]
                    all_pages = [
                        page
                        for page in pages
                        if page.get("type") == "page" and page.get("webSocketDebuggerUrl")
                    ]
                    selected = (chat_pages or zhipin_pages or all_pages or [None])[0]
                    if selected:
                        return str(selected["webSocketDebuggerUrl"])
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)
        raise BossConnectorError(f"Chrome CDP page endpoint did not start: {last_error}")

    def _is_cdp_alive(self, port: int) -> bool:
        try:
            self._cdp_websocket_url(port, timeout_s=0.5)
            return True
        except Exception:
            return False

    @staticmethod
    def _open_or_activate_cdp_url(port: int, url: str) -> None:
        """Bring the requested BOSS tab forward, or create that exact URL."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                pages = json.loads(response.read().decode("utf-8"))
            for page in pages if isinstance(pages, list) else []:
                page_url = str(page.get("url") or "")
                page_id = str(page.get("id") or "")
                if not page_id or not page_url.startswith(url):
                    continue
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/json/activate/{page_id}",
                    method="PUT",
                )
                with opener.open(request, timeout=2):
                    return
        except Exception:
            pass

        encoded_url = urllib.parse.quote(url, safe=":/?=&")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{encoded_url}",
            method="PUT",
        )
        try:
            with opener.open(request, timeout=3):
                return
        except Exception as exc:
            raise BossConnectorError(
                f"Unable to open BOSS login tab through Chrome CDP: {exc}"
            ) from exc

    def _read_debug_port(self) -> int | None:
        try:
            raw = self.debug_port_file.read_text(encoding="utf-8").strip()
            port = int(raw)
            if 0 < port < 65536:
                return port
        except Exception:
            return None
        return None

    def _write_debug_port(self, port: int) -> None:
        try:
            self.debug_port_file.write_text(str(port), encoding="utf-8")
        except Exception:
            pass

    def _launch_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": False,
            "viewport": {"width": 1440, "height": 920},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": self._user_agent(),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        executable = self._browser_executable()
        if executable is not None:
            options["executable_path"] = str(executable)
        return options

    def _browser_executable(self) -> Path | None:
        env_path = os.environ.get("CAPYBOT_BOSS_BROWSER")
        candidates = [Path(env_path).expanduser()] if env_path else []
        candidates.extend(
            [
                Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("ProgramFiles(x86)", ""))
                / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
                Path(os.environ.get("ProgramFiles(x86)", ""))
                / "Microsoft/Edge/Application/msedge.exe",
            ]
        )
        for candidate in candidates:
            if candidate and candidate.exists():
                return candidate
        return None

    @staticmethod
    def _user_agent() -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        )

    def _boss_id_for_index(self, index: int) -> str | None:
        page = self._ensure_page()
        page.evaluate("(i) => document.querySelectorAll('.friend-content')[i]?.click()", index)
        page.wait_for_timeout(1800)
        return page.evaluate(
            """
            () => {
              const entries = performance.getEntriesByType('resource');
              for (let i = entries.length - 1; i >= 0; i--) {
                const url = entries[i].name || '';
                if (!url.includes('/wapi/zpchat/geek/historyMsg')) continue;
                const match = url.match(/bossId=([^&]+)/);
                if (match) return decodeURIComponent(match[1]);
              }
              return null;
            }
            """
        )

    async def _boss_id_for_index_async(self, index: int) -> str | None:
        identity = await self._conversation_identity_for_index_async(index)
        return str(identity.get("boss_uid") or "") or None

    async def _conversation_identity_for_index_async(
        self,
        index: int,
    ) -> dict[str, Any]:
        page = await self._ensure_async_page()
        page = await self._ensure_chat_page_async(page)
        await page.evaluate("() => performance.clearResourceTimings()")
        clicked = await page.evaluate(
            """
            (i) => {
              const el = document.querySelectorAll('.friend-content')[i];
              if (!el) return false;
              el.scrollIntoView({ block: 'center' });
              el.click();
              return true;
            }
            """,
            index,
        )
        if not clicked:
            return {}
        await page.wait_for_timeout(2500)
        page = await self._ensure_chat_page_async(page)
        result = await page.evaluate(
            """
            () => {
              const entries = performance.getEntriesByType('resource');
              for (let i = entries.length - 1; i >= 0; i--) {
                const url = entries[i].name || '';
                if (!url.includes('/wapi/zpchat/geek/historyMsg')) continue;
                const parsed = new URL(url);
                return {
                  boss_uid: parsed.searchParams.get('bossId') || '',
                  security_id: parsed.searchParams.get('securityId') || '',
                  group_id: parsed.searchParams.get('groupId') || parsed.searchParams.get('gid') || '',
                  friend_source: Number(parsed.searchParams.get('src') || 0)
                };
              }
              return {};
            }
            """
        )
        return self._conversation_identity_from_row(result)

    @staticmethod
    def _conversation_identity_from_row(row: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        return {
            "boss_uid": str(
                row.get("boss_uid") or row.get("encryptBossId") or row.get("encrypt_boss_id") or ""
            ),
            "security_id": str(row.get("security_id") or row.get("securityId") or ""),
            "group_id": str(row.get("group_id") or row.get("groupId") or row.get("gid") or ""),
            "friend_source": int(
                row.get("friend_source") or row.get("friendSource") or row.get("source") or 0
            ),
            "unique_id": str(row.get("unique_id") or row.get("uniqueId") or ""),
            "current_job_id": str(
                row.get("current_job_id") or row.get("encryptJobId") or row.get("jobId") or ""
            ),
        }

    async def _ensure_chat_page_async(self, page: Any) -> Any:
        for attempt in range(2):
            try:
                url = await page.current_url() if isinstance(page, RawCDPPage) else (page.url or "")
                if "/web/geek/chat" in url:
                    return page
                if "zhipin.com" in url:
                    raise BossConnectorError(
                        "BOSS 页面已离开聊天页，导入已暂停。通常是登录态失效、页面风控或跳转导致。"
                        f" 当前页面：{url}。请在弹出的 Chrome 中登录并确认聊天页稳定显示后再导入。"
                    )
                await page.goto(BOSS_CHAT_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                next_url = (
                    await page.current_url() if isinstance(page, RawCDPPage) else (page.url or "")
                )
                if "/web/geek/chat" not in next_url:
                    raise BossConnectorError(
                        "BOSS 聊天页不可读。请先在 Capybot 弹出的 Chrome 中完成 BOSS 登录，"
                        f"并确认聊天页能稳定显示后再导入。当前页面：{next_url or url}"
                    )
                return page
            except Exception as exc:
                if (
                    attempt
                    or not isinstance(page, RawCDPPage)
                    or not self._is_transient_cdp_error(exc)
                ):
                    raise
                await self._discard_async_page()
                page = await self._ensure_async_page()
        return page

    @staticmethod
    def _first_token(text: str) -> str:
        return (text or "BOSS 联系人").split(" ")[0][:24]

    @staticmethod
    def _job_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
        body = message.get("body") or {}
        job = body.get("jobDesc")
        if not isinstance(job, dict):
            return None
        boss = job.get("boss") or {}
        return {
            "platform_job_id": str(job.get("encryptJobId") or job.get("jobId") or ""),
            "title": job.get("title") or "待补全岗位",
            "company": job.get("company"),
            "salary": job.get("salary"),
            "city": job.get("city"),
            "experience": job.get("experience"),
            "education": job.get("education"),
            "boss_uid": str(boss.get("uid") or message.get("from", {}).get("uid") or ""),
            "boss_name": boss.get("name"),
            "raw_payload": job,
        }

    def _fixture_job_detail(self, platform_job_id: str) -> dict[str, Any]:
        job_id = str(platform_job_id or "").strip()
        details = self._fixture.get("job_details") or {}
        if isinstance(details, dict) and isinstance(details.get(job_id), dict):
            return self._unwrap_job_detail(details[job_id])
        for messages in (self._fixture.get("messages") or {}).values():
            for message in messages if isinstance(messages, list) else []:
                job = self._job_from_message(message)
                if not job:
                    continue
                raw = dict(job.get("raw_payload") or {})
                security_ids = {
                    str(raw.get("securityId") or "").strip(),
                    str(raw.get("_capybot_security_id") or "").strip(),
                }
                for key in ("url", "jobUrl", "job_url"):
                    url = str(raw.get(key) or "").strip()
                    if url:
                        security_ids.update(
                            urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(
                                "securityId", []
                            )
                        )
                lookup_ids = {
                    str(job.get("platform_job_id") or "").strip(),
                    *security_ids,
                }
                if job_id not in lookup_ids:
                    continue
                return {
                    "jobInfo": {
                        "encryptJobId": job_id,
                        "jobName": job.get("title"),
                        "salaryDesc": job.get("salary"),
                        "cityName": job.get("city"),
                        "experienceName": job.get("experience"),
                        "degreeName": job.get("education"),
                        "postDescription": raw.get("postDescription") or raw.get("description"),
                        "jobLabels": raw.get("jobLabels") or raw.get("skills") or [],
                    },
                    "brandComInfo": {"brandName": job.get("company")},
                    "bossInfo": {
                        "name": job.get("boss_name"),
                        "encryptBossId": job.get("boss_uid"),
                    },
                    "jobDetail": raw.get("jobDetail")
                    or raw.get("postDescription")
                    or raw.get("description")
                    or "",
                }
        raise BossConnectorError(f"BOSS fixture has no job detail: {job_id}")

    @staticmethod
    def _unwrap_job_detail(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise BossConnectorError("BOSS job detail response is not an object")
        code = data.get("code")
        if code not in {None, 0}:
            raise BossConnectorError(
                str(data.get("message") or data.get("msg") or "BOSS job detail API failed")
            )
        payload = data.get("zpData") or data.get("data") or data
        if not isinstance(payload, dict):
            raise BossConnectorError("BOSS job detail response has no payload")
        if not isinstance(payload.get("jobInfo"), dict):
            raise BossConnectorError("BOSS job detail is empty or the job is offline")
        return payload
