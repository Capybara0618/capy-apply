"""Structured public company research for a single job opportunity."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx
from ddgs import DDGS

from capybot.apply.evidence import EvidenceRepository
from capybot.apply.models import utc_now_iso
from capybot.apply.store import ApplyStore
from capybot.connectors.boss import BossConnector

SearchFunction = Callable[[str, int], list[dict[str, Any]]]
FetchFunction = Callable[[str], Awaitable["FetchedPage"]]
BossCompanySearchFunction = Callable[[str, int], Awaitable[list[dict[str, str]]]]

COMPANY_FOCUSES = frozenset({"basic", "business", "employment"})


@dataclass(frozen=True)
class OpportunityIdentity:
    opportunity_id: str
    company: str


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    text: str
    content_type: str


class CompanyIntelligenceService:
    """Research company evidence without accepting free-form queries."""

    def __init__(
        self,
        store: ApplyStore | None = None,
        *,
        repository: EvidenceRepository | None = None,
        searcher: SearchFunction | None = None,
        fetcher: FetchFunction | None = None,
        boss_company_searcher: BossCompanySearchFunction | None = None,
        search_timeout_seconds: float = 8.0,
    ) -> None:
        self.store = store or ApplyStore()
        self.repository = repository or EvidenceRepository(self.store)
        self.searcher = searcher or _search_web
        self.fetcher = fetcher or fetch_public_page
        self.boss_company_searcher = (
            boss_company_searcher
            if boss_company_searcher is not None
            else (_search_boss_company_jobs if searcher is None else _empty_boss_company_search)
        )
        self.search_timeout_seconds = search_timeout_seconds

    async def research_company(
        self,
        opportunity_id: str,
        *,
        focus: str = "basic",
    ) -> dict[str, Any]:
        if focus not in COMPANY_FOCUSES:
            raise ValueError(f"不支持的公司研究方向: {focus}")
        identity = self._identity(opportunity_id)
        if not identity.company:
            return self._missing_context(opportunity_id, "当前机会缺少公司名称")
        queries = _company_queries(identity, focus)
        return await self._research(
            identity,
            research_type=f"company:{focus}",
            queries=queries,
            anchor_terms=_company_aliases(identity.company),
        )

    def _identity(self, opportunity_id: str) -> OpportunityIdentity:
        self.store.bind_opportunity_account(opportunity_id)
        context = self.store.opportunity_context(opportunity_id)
        opportunity = context.get("opportunity")
        if not opportunity:
            raise ValueError("当前账号下不存在该机会")
        return OpportunityIdentity(
            opportunity_id=opportunity_id,
            company=_clean_entity(opportunity.get("company")),
        )

    async def _research(
        self,
        identity: OpportunityIdentity,
        *,
        research_type: str,
        queries: list[str],
        anchor_terms: list[str],
        max_sources: int = 4,
    ) -> dict[str, Any]:
        cache_loader = getattr(self.repository, "recent_research_sources", None)
        cached_sources = (
            cache_loader(
                identity.opportunity_id,
                research_type=research_type,
                limit=max_sources,
            )
            if callable(cache_loader)
            else []
        )
        if cached_sources:
            refs = [f"web_source:{source['id']}" for source in cached_sources]
            return {
                "ok": True,
                "opportunity_id": identity.opportunity_id,
                "research_type": research_type,
                "finding_status": "found",
                "summary": f"复用 {len(cached_sources)} 条 24 小时内的公开来源。",
                "facts": [
                    {
                        "kind": "research_status",
                        "status": "found",
                        "research_type": research_type,
                        "source_count": len(cached_sources),
                        "verified_count": sum(
                            bool(source["verified"]) for source in cached_sources
                        ),
                        "search_error_count": 0,
                        "cache_hit": True,
                    },
                    *cached_sources,
                ],
                "evidence_refs": refs,
                "freshness": "cached_within_24h",
            }

        candidates: list[tuple[str, dict[str, Any]]] = []
        seen_urls: set[str] = set()
        search_errors: list[str] = []
        for query in queries:
            try:
                rows = await asyncio.wait_for(
                    asyncio.to_thread(self.searcher, query, max_sources * 3),
                    timeout=self.search_timeout_seconds,
                )
            except Exception as exc:
                search_errors.append(_clean_text(str(exc) or type(exc).__name__, 160))
                continue
            for row in rows:
                url = str(row.get("href") or row.get("url") or "").strip()
                if url in seen_urls or not url.startswith(("http://", "https://")):
                    continue
                if not _is_relevant(
                    row,
                    identity=identity,
                ):
                    continue
                seen_urls.add(url)
                candidates.append((query, row))
                if len(candidates) >= max_sources * 2:
                    break
            if len(candidates) >= max_sources * 2:
                break

        sources: list[dict[str, Any]] = []
        for query, row in candidates:
            if len(sources) >= max_sources:
                break
            source = await self._verify_and_store(
                identity,
                research_type=research_type,
                query=query,
                row=row,
                anchor_terms=anchor_terms,
            )
            if source:
                sources.append(source)

        if not sources:
            try:
                boss_rows = await asyncio.wait_for(
                    self.boss_company_searcher(identity.company, max_sources),
                    timeout=12.0,
                )
            except Exception as exc:
                search_errors.append(_clean_text(str(exc) or type(exc).__name__, 160))
            else:
                for row in boss_rows[:max_sources]:
                    source = self._store_boss_company_source(
                        identity,
                        research_type=research_type,
                        row=row,
                    )
                    if source:
                        sources.append(source)

        refs = [f"web_source:{source['id']}" for source in sources]
        finding_status = "found" if sources else ("unavailable" if search_errors else "not_found")
        return {
            "ok": True,
            "opportunity_id": identity.opportunity_id,
            "research_type": research_type,
            "finding_status": finding_status,
            "summary": (
                f"找到 {len(sources)} 条相关公开来源；"
                f"其中 {sum(bool(source['verified']) for source in sources)} 条已通过来源校验。"
                if sources
                else (
                    "公开搜索服务暂时不可用，未完成外部证据核验。"
                    if finding_status == "unavailable"
                    else "未找到满足公司实体约束的公开来源；这不代表相关事实不存在。"
                )
            ),
            "facts": [
                {
                    "kind": "research_status",
                    "status": finding_status,
                    "research_type": research_type,
                    "source_count": len(sources),
                    "verified_count": sum(bool(source["verified"]) for source in sources),
                    "search_error_count": len(search_errors),
                },
                *sources,
            ],
            "evidence_refs": refs,
            "freshness": utc_now_iso(),
        }

    def _store_boss_company_source(
        self,
        identity: OpportunityIdentity,
        *,
        research_type: str,
        row: dict[str, str],
    ) -> dict[str, Any] | None:
        url = str(row.get("href") or "")
        excerpt = _sanitize_public_excerpt(str(row.get("summary") or ""), 1200)
        if not url.startswith("https://www.zhipin.com/") or not excerpt:
            return None
        stored = self.repository.save_research_source(
            identity.opportunity_id,
            query=f"BOSS 公司职位：{identity.company}",
            url=url,
            title=str(row.get("title") or identity.company),
            excerpt=excerpt,
            research_type=research_type,
            source_tier="recruitment",
            quality_score=0.76,
            verified=True,
            metadata={
                "content_trust": "untrusted_public_web",
                "verification": "rendered_boss_search",
                "search_provider": "boss",
                "query_template_version": 1,
            },
        )
        return {
            "kind": "public_source",
            "id": stored["id"],
            "title": stored["title"],
            "url": stored["url"],
            "source_domain": stored["source_domain"],
            "excerpt": stored["excerpt"],
            "research_type": research_type,
            "source_tier": "recruitment",
            "quality_score": 0.76,
            "verified": True,
            "trust": "untrusted_public_web",
        }

    async def _verify_and_store(
        self,
        identity: OpportunityIdentity,
        *,
        research_type: str,
        query: str,
        row: dict[str, Any],
        anchor_terms: list[str],
    ) -> dict[str, Any] | None:
        url = str(row.get("href") or row.get("url") or "")
        search_title = _clean_text(str(row.get("title") or ""), 300)
        search_excerpt = _sanitize_public_excerpt(
            str(row.get("body") or row.get("snippet") or ""),
            1200,
        )
        verified = False
        final_url = url
        title = search_title
        excerpt = search_excerpt
        fetch_error: str | None = None
        try:
            page = await self.fetcher(url)
            page_haystack = f"{page.title} {page.text}"
            if len(page.text) >= 80 and _contains_company(page_haystack, identity.company):
                verified = True
                final_url = page.url
                title = page.title or title
                excerpt = _focused_excerpt(page.text, anchor_terms)
            else:
                fetch_error = "页面正文未通过实体相关性校验"
        except Exception as exc:
            fetch_error = _clean_text(str(exc), 240)

        if not excerpt:
            return None
        tier, quality = _source_quality(final_url, verified)
        stored = self.repository.save_research_source(
            identity.opportunity_id,
            query=query,
            url=final_url,
            title=title or None,
            excerpt=excerpt,
            research_type=research_type,
            source_tier=tier,
            quality_score=quality,
            verified=verified,
            metadata={
                "content_trust": "untrusted_public_web",
                "fetch_error": fetch_error,
                "query_template_version": 1,
                "search_provider": row.get("_provider") or "custom",
            },
        )
        return {
            "kind": "public_source",
            "id": stored["id"],
            "title": stored["title"],
            "url": stored["url"],
            "source_domain": stored["source_domain"],
            "excerpt": stored["excerpt"],
            "research_type": research_type,
            "source_tier": tier,
            "quality_score": quality,
            "verified": verified,
            "trust": "untrusted_public_web",
        }

    @staticmethod
    def _missing_context(opportunity_id: str, reason: str) -> dict[str, Any]:
        return {
            "ok": True,
            "opportunity_id": opportunity_id,
            "finding_status": "needs_context",
            "summary": reason,
            "facts": [
                {
                    "kind": "research_status",
                    "status": "needs_context",
                    "reason": reason,
                }
            ],
            "evidence_refs": [],
            "freshness": "missing_context",
        }


async def fetch_public_page(url: str) -> FetchedPage:
    """Fetch a small public HTML page after validating every redirect target."""

    current_url = url
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 CapybotOpportunityIntel/1.0"
            )
        },
    ) as client:
        for _ in range(4):
            await _assert_public_url(current_url)
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("公开页面重定向缺少地址")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not any(value in content_type for value in ("text/html", "text/plain")):
                    raise ValueError("公开来源不是可读取的文本页面")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 512_000:
                        raise ValueError("公开页面超过 500KB 读取上限")
                    chunks.append(chunk)
                body = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                decoded = body.decode(encoding, errors="replace")
                title, text = _extract_html(decoded, content_type)
                return FetchedPage(
                    url=str(response.url),
                    title=title,
                    text=text,
                    content_type=content_type,
                )
    raise ValueError("公开页面重定向次数过多")


async def _search_boss_company_jobs(
    company: str,
    limit: int,
) -> list[dict[str, str]]:
    connector = BossConnector()
    try:
        return await connector.search_company_jobs_async(company, limit=limit)
    finally:
        await connector.close_async()


async def _empty_boss_company_search(
    _company: str,
    _limit: int,
) -> list[dict[str, str]]:
    return []


async def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许访问公开 HTTP/HTTPS 页面")
    if parsed.username or parsed.password:
        raise ValueError("公开来源 URL 不允许包含认证信息")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("公开来源 URL 使用了非标准端口")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("拒绝访问本机地址")
    try:
        direct_ip = ipaddress.ip_address(host)
        addresses = [direct_ip]
    except ValueError:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise ValueError("拒绝访问内网、环回或保留地址")


def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.title_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.skip_depth += 1
        if tag == "title":
            self.title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "template"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title" and self.title_depth:
            self.title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.title_depth:
            self.title_parts.append(data)
        self.text_parts.append(data)


def _extract_html(value: str, content_type: str) -> tuple[str, str]:
    if "text/plain" in content_type:
        return "", _clean_text(value, 30_000)
    parser = _VisibleTextParser()
    parser.feed(value)
    return (
        _clean_text(" ".join(parser.title_parts), 300),
        _clean_text(" ".join(parser.text_parts), 30_000),
    )


def _search_web(query: str, limit: int) -> list[dict[str, Any]]:
    """Search through independent providers and return the first useful result set."""

    errors: list[str] = []
    for provider in (_search_bing, _search_ddgs):
        try:
            rows = provider(query, limit)
        except Exception as exc:
            errors.append(f"{provider.__name__}: {exc}")
            continue
        if rows:
            return rows
    if errors:
        raise RuntimeError("；".join(errors))
    return []


def _search_ddgs(query: str, limit: int) -> list[dict[str, Any]]:
    with DDGS() as ddgs:
        return [{**row, "_provider": "ddgs"} for row in ddgs.text(query, max_results=limit)]


def _search_bing(query: str, limit: int) -> list[dict[str, Any]]:
    response = httpx.get(
        "https://cn.bing.com/search",
        params={"q": query, "count": min(30, max(5, limit))},
        timeout=httpx.Timeout(8.0, connect=4.0),
        follow_redirects=True,
        trust_env=False,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    response.raise_for_status()
    parser = _BingSearchParser(limit=limit)
    parser.feed(response.text)
    return parser.rows


class _BingSearchParser(HTMLParser):
    """Extract Bing organic results without depending on brittle regex parsing."""

    def __init__(self, *, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.rows: list[dict[str, Any]] = []
        self._result_depth = 0
        self._in_heading = False
        self._in_excerpt = False
        self._href = ""
        self._title_parts: list[str] = []
        self._excerpt_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "li" and "b_algo" in classes and not self._result_depth:
            self._result_depth = 1
            self._href = ""
            self._title_parts = []
            self._excerpt_parts = []
            return
        if not self._result_depth:
            return
        self._result_depth += 1
        if tag == "h2":
            self._in_heading = True
        elif tag == "a" and self._in_heading and not self._href:
            self._href = values.get("href", "")
        elif tag == "p":
            self._in_excerpt = True

    def handle_endtag(self, tag: str) -> None:
        if not self._result_depth:
            return
        if tag == "h2":
            self._in_heading = False
        elif tag == "p":
            self._in_excerpt = False
        self._result_depth -= 1
        if not self._result_depth:
            self._finish_result()

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._title_parts.append(data)
        elif self._in_excerpt:
            self._excerpt_parts.append(data)

    def _finish_result(self) -> None:
        href = unescape(self._href).strip()
        title = _clean_text(" ".join(self._title_parts), 300)
        excerpt = _clean_text(" ".join(self._excerpt_parts), 1200)
        if len(self.rows) < self.limit and href.startswith(("http://", "https://")) and title:
            self.rows.append(
                {
                    "title": title,
                    "body": excerpt,
                    "href": href,
                    "_provider": "bing",
                }
            )


def _is_relevant(
    row: dict[str, Any],
    *,
    identity: OpportunityIdentity,
) -> bool:
    haystack = " ".join(
        str(row.get(key) or "") for key in ("title", "body", "snippet", "href", "url")
    )
    return _contains_company(haystack, identity.company)


def _contains_company(haystack: str, company: str) -> bool:
    normalized = _normalize(haystack)
    return any(_normalize(alias) in normalized for alias in _company_aliases(company))


def _company_queries(identity: OpportunityIdentity, focus: str) -> list[str]:
    suffixes = {
        "basic": ("官网 公司简介", "企查查 天眼查"),
        "business": ("主营业务 产品 服务", "公司介绍 业务"),
        "employment": ("招聘 员工 公司", "招聘 评价"),
    }
    return [f'"{identity.company}" {suffix}' for suffix in suffixes[focus]]


def _company_aliases(company: str) -> list[str]:
    if not company:
        return []
    aliases = {company}
    shortened = re.sub(
        r"(有限责任公司|股份有限公司|有限公司|科技公司|网络科技|信息技术|集团)$",
        "",
        company,
    ).strip()
    if len(shortened) >= 2:
        aliases.add(shortened)
    return sorted(aliases, key=len, reverse=True)


def _source_quality(url: str, verified: bool) -> tuple[str, float]:
    domain = (urlparse(url).hostname or "").casefold()
    if domain.endswith(".gov.cn") or domain == "gov.cn":
        tier, score = "authority", 0.92
    elif any(
        value in domain
        for value in ("qcc.com", "tianyancha.com", "aiqicha.baidu.com", "aiqicha.com")
    ):
        tier, score = "registry", 0.78
    elif any(value in domain for value in ("zhipin.com", "liepin.com", "lagou.com")):
        tier, score = "recruitment", 0.68
    elif any(
        value in domain
        for value in (
            "36kr.com",
            "thepaper.cn",
            "people.com.cn",
            "zjol.com.cn",
            "sina.com.cn",
        )
    ):
        tier, score = "media", 0.7
    elif any(value in domain for value in ("zhihu.com", "weibo.com", "tieba.baidu.com")):
        tier, score = "community", 0.35
    else:
        tier, score = "general", 0.48
    return tier, min(1.0, score + (0.08 if verified else 0.0))


def _focused_excerpt(text: str, terms: list[str], limit: int = 1200) -> str:
    clean = _clean_text(text, 30_000)
    normalized_terms = [term for term in terms if len(term) >= 2]
    positions = [clean.casefold().find(term.casefold()) for term in normalized_terms]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return _sanitize_public_excerpt(clean, limit)
    start = max(0, min(positions) - 260)
    return _sanitize_public_excerpt(clean[start : start + limit], limit)


def _clean_entity(value: Any) -> str:
    return re.sub(r"[\x00-\x1f\"'`<>]+", " ", str(value or "")).strip()[:100]


def _clean_text(value: str, limit: int) -> str:
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", value)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _sanitize_public_excerpt(value: str, limit: int) -> str:
    """Remove common prompt-injection phrases before an excerpt reaches the model."""

    text = _clean_text(value, limit * 2)
    patterns = (
        r"(?i)ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?",
        r"(?i)system\s+prompt",
        r"忽略.{0,30}(?:指令|要求|提示)",
        r"系统提示词",
        r"你现在是.{0,50}(?:助手|模型|agent)",
    )
    for pattern in patterns:
        text = re.sub(pattern, "[已移除可疑网页指令]", text)
    return text[:limit]


def _normalize(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())
