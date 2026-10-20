"""해외 바이오 뉴스 → 텔레그램 알림 봇

companies.yaml 의 기업(글로벌 빅파마 + 국내 제약·바이오)을 대상으로
구글 뉴스(영문)와 해외 바이오 전문지 RSS에서 새 기사를 찾아 텔레그램으로 링크를 보냅니다.

실행:
    python main.py              # 수집 후 텔레그램 전송 (환경변수 필요)
    python main.py --dry-run    # 전송하지 않고 화면에 출력만

필요한 환경변수 (GitHub 저장소 Settings > Secrets 에 등록):
    TELEGRAM_BOT_TOKEN   BotFather 에서 받은 봇 토큰
    TELEGRAM_CHAT_ID     메시지를 받을 채팅/채널 ID (여러 개면 쉼표로 구분)
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
import yaml
from dateutil import parser as dateparser
from dateutil import tz

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "sent.json"
KST = timezone(timedelta(hours=9))
US_EASTERN = tz.gettz("America/New_York")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TELEGRAM_LIMIT = 3800  # 텔레그램 한 메시지 최대 4096자, 여유를 둠


# ---------------------------------------------------------------------------
# 기업 목록
# ---------------------------------------------------------------------------

@dataclass
class Company:
    name: str
    label: str  # 알림에 보일 이름
    region: str  # "korea" | "global"
    terms: list[str]
    needs_context: bool
    patterns: list[re.Pattern] = field(default_factory=list)

    def __post_init__(self) -> None:
        for t in self.terms:
            escaped = re.escape(t)
            # 영문·숫자 경계로 단어 단위 매칭 (Lilly 가 Lillyana 에 걸리지 않게)
            pat = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
            # 짧은 대문자 약칭(BMS, GSK, HLB …)은 대소문자를 구분해 오탐을 줄임
            short_acronym = t.isupper() and len(t) <= 5
            flags = 0 if short_acronym else re.IGNORECASE
            self.patterns.append(re.compile(pat, flags))

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns)


def load_companies(path: Path) -> list[Company]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    companies: list[Company] = []
    for region in ("korea", "global"):
        for row in data.get(region) or []:
            name = str(row["name"]).strip()
            aliases = [str(a).strip() for a in row.get("aliases") or []]
            label = str(row.get("ko") or name)
            companies.append(
                Company(
                    name=name,
                    label=label,
                    region=region,
                    terms=[name] + aliases,
                    needs_context=bool(row.get("ctx")),
                )
            )
    return companies


# ---------------------------------------------------------------------------
# 기사 수집
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str = ""
    companies: list[Company] = field(default_factory=list)

    @property
    def key(self) -> str:
        return normalize_title(self.title)


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:90]


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        st = entry.get(attr)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            dt = dateparser.parse(raw)
            if dt.tzinfo is None:  # 시간대 없는 날짜(예: Fierce)는 미국 동부시간으로 간주
                dt = dt.replace(tzinfo=US_EASTERN)
            return dt.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return None
    return None


def http_get(url: str, retries: int = 2) -> bytes | None:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            if r.status_code == 200:
                return r.content
            print(f"  ! HTTP {r.status_code}: {url[:120]}", file=sys.stderr)
            if r.status_code not in (429, 500, 502, 503, 504):
                return None
        except requests.RequestException as e:
            print(f"  ! 요청 실패 ({e.__class__.__name__}): {url[:120]}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    return None


def build_google_queries(companies: list[Company], max_chars: int) -> list[str]:
    """기업 검색어를 OR로 묶어 구글 뉴스 검색어 여러 개로 나눔."""
    queries: list[str] = []
    current: list[str] = []
    for c in companies:
        terms = [f'"{t}"' for t in c.terms]
        candidate = current + terms
        if current and len(" OR ".join(candidate)) > max_chars:
            queries.append("(" + " OR ".join(current) + ") when:1d")
            current = terms
        else:
            current = candidate
    if current:
        queries.append("(" + " OR ".join(current) + ") when:1d")
    return queries


def fetch_google_news(companies: list[Company], cfg: dict) -> list[Article]:
    queries = build_google_queries(companies, int(cfg.get("max_query_chars", 350)))
    delay = float(cfg.get("delay_seconds", 1.5))
    print(f"구글 뉴스 검색 {len(queries)}건")
    articles: list[Article] = []
    for i, q in enumerate(queries, 1):
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(q)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        content = http_get(url)
        if content:
            feed = feedparser.parse(content)
            for e in feed.entries:
                source = (e.get("source") or {}).get("title") or "Google News"
                title = strip_html(e.get("title", ""))
                # 구글 뉴스 제목 끝의 " - 매체명" 제거
                suffix = f" - {source}"
                if title.endswith(suffix):
                    title = title[: -len(suffix)]
                articles.append(
                    Article(
                        title=title,
                        link=e.get("link", ""),
                        source=source,
                        published=parse_date(e),
                        summary="",  # 구글 뉴스 요약은 제목 반복이라 사용하지 않음
                    )
                )
        if i < len(queries):
            time.sleep(delay)
    return articles


def fetch_feeds(feeds: list[dict]) -> list[Article]:
    articles: list[Article] = []
    for f in feeds:
        content = http_get(f["url"])
        if not content:
            continue
        feed = feedparser.parse(content)
        print(f"{f['name']}: {len(feed.entries)}건")
        for e in feed.entries:
            articles.append(
                Article(
                    title=strip_html(e.get("title", "")),
                    link=e.get("link", ""),
                    source=f["name"],
                    published=parse_date(e),
                    summary=strip_html(e.get("summary", ""))[:400],
                )
            )
    return articles


# ---------------------------------------------------------------------------
# 필터링
# ---------------------------------------------------------------------------

def make_context_pattern(words: list[str]) -> re.Pattern:
    alts = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)


def tag_companies(articles: list[Article], companies: list[Company], ctx: re.Pattern) -> list[Article]:
    kept: list[Article] = []
    for a in articles:
        text = f"{a.title} {a.summary}"
        has_ctx = bool(ctx.search(text))
        a.companies = [c for c in companies if c.matches(text) and (has_ctx or not c.needs_context)]
        # 같은 기사에서 짧은 이름이 긴 이름의 일부일 때 중복 태그 정리
        # (예: "Merck KGaA" 기사에 "Merck" 까지 붙지 않도록)
        if len(a.companies) > 1:
            a.companies = [
                c for c in a.companies
                if not any(
                    o is not c and len(o.name) > len(c.name)
                    and c.name.lower() in o.name.lower()
                    for o in a.companies
                )
            ]
        if a.companies:
            kept.append(a)
    return kept


def select_new(articles: list[Article], state: dict, lookback_hours: float, first_run: bool) -> list[Article]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    merged: dict[str, Article] = {}
    for a in articles:
        if not a.key or not a.link:
            continue
        if a.key in state or a.link in state:
            continue
        if a.published is None:
            # 날짜 없는 기사는 첫 실행 때 기록만 하고 보내지 않음 (첫 알림 폭주 방지)
            if first_run:
                state[a.key] = now.isoformat()
                continue
        elif a.published < cutoff:
            continue
        if a.key in merged:  # 같은 기사가 여러 경로로 들어오면 기업 태그만 합침
            existing = merged[a.key]
            for c in a.companies:
                if c not in existing.companies:
                    existing.companies.append(c)
            # 전문지 원문 링크를 구글 뉴스 링크보다 우선
            if "news.google.com" in existing.link and "news.google.com" not in a.link:
                existing.link, existing.source = a.link, a.source
            continue
        merged[a.key] = a
    return sorted(
        merged.values(),
        key=lambda x: x.published or now,
        reverse=True,
    )


# ---------------------------------------------------------------------------
# 상태 파일 (보낸 기사 기록)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("! sent.json 이 손상되어 새로 시작합니다", file=sys.stderr)
    return {}


def save_state(state: dict, keep_days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    pruned = {}
    for k, v in state.items():
        try:
            if datetime.fromisoformat(v) >= cutoff:
                pruned[k] = v
        except ValueError:
            continue
    STATE_FILE.write_text(
        json.dumps(pruned, ensure_ascii=False, indent=0, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 텔레그램
# ---------------------------------------------------------------------------

def format_line(a: Article) -> str:
    labels = ", ".join(html.escape(c.label) for c in a.companies[:3])
    if len(a.companies) > 3:
        labels += f" 외 {len(a.companies) - 3}"
    when = a.published.astimezone(KST).strftime("%m/%d %H:%M") if a.published else ""
    meta = html.escape(a.source) + (f" · {when}" if when else "")
    return (
        f"• <b>[{labels}]</b> "
        f'<a href="{html.escape(a.link, quote=True)}">{html.escape(a.title)}</a>\n'
        f"   <i>{meta}</i>"
    )


def build_messages(articles: list[Article]) -> list[str]:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    korea = [a for a in articles if any(c.region == "korea" for c in a.companies)]
    global_ = [a for a in articles if a not in korea]

    blocks: list[str] = [f"🧬 <b>해외 바이오 뉴스</b> ({now} KST · {len(articles)}건)"]
    if korea:
        blocks.append(f"\n🇰🇷 <b>국내 기업 해외 보도</b> ({len(korea)})")
        blocks += [format_line(a) for a in korea]
    if global_:
        blocks.append(f"\n🌍 <b>글로벌 제약·바이오</b> ({len(global_)})")
        blocks += [format_line(a) for a in global_]

    messages: list[str] = []
    current = ""
    for b in blocks:
        if current and len(current) + len(b) + 1 > TELEGRAM_LIMIT:
            messages.append(current)
            current = b
        else:
            current = f"{current}\n{b}" if current else b
    if current:
        messages.append(current)
    return messages


def send_telegram(token: str, chat_ids: list[str], messages: list[str]) -> bool:
    ok = True
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        for text in messages:
            for attempt in range(3):
                r = requests.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=20,
                )
                if r.status_code == 200:
                    break
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(int(wait) + 1)
                    continue
                print(f"! 텔레그램 전송 실패 ({chat_id}): {r.status_code} {r.text[:300]}", file=sys.stderr)
                ok = False
                break
            time.sleep(1)  # 텔레그램 전송 속도 제한 대응
    return ok


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="전송하지 않고 출력만")
    args = ap.parse_args()

    config = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    settings = config.get("settings") or {}
    companies = load_companies(ROOT / "companies.yaml")
    ctx = make_context_pattern(config.get("context_words") or [])
    print(f"대상 기업: 국내 {sum(c.region == 'korea' for c in companies)} · "
          f"글로벌 {sum(c.region == 'global' for c in companies)}")

    raw: list[Article] = []
    gn = config.get("google_news") or {}
    if gn.get("enabled", True):
        raw += fetch_google_news(companies, gn)
    raw += fetch_feeds(config.get("feeds") or [])
    print(f"수집한 기사: {len(raw)}건")

    tagged = tag_companies(raw, companies, ctx)
    state = load_state()
    first_run = not state
    new = select_new(tagged, state, float(settings.get("lookback_hours", 6)), first_run)
    print(f"대상 기업 관련 새 기사: {len(new)}건")

    if not new and not settings.get("send_when_empty", False):
        save_state(state, int(settings.get("keep_days", 14)))
        return 0

    messages = (
        build_messages(new) if new
        else [f"🧬 해외 바이오 뉴스 ({datetime.now(KST):%m/%d %H:%M} KST): 새 소식 없음"]
    )

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

    if args.dry_run or not token or not chat_ids:
        if not args.dry_run:
            print("! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없어 출력만 합니다", file=sys.stderr)
        for m in messages:
            print("-" * 60)
            print(m)
        return 0 if args.dry_run else 1

    if not send_telegram(token, chat_ids, messages):
        return 1  # 전송 실패 시 기록하지 않아 다음 실행 때 다시 시도

    now = datetime.now(timezone.utc).isoformat()
    for a in new:
        state[a.key] = now
        state[a.link] = now
    save_state(state, int(settings.get("keep_days", 14)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
