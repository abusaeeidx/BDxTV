import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser


ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://streamecenter.live"
ALT_BASE = "https://streame.center"

TAG = "STRMCNTR"

CACHE_DIR = ROOT_DIR / "caches"
CACHE_FILE = CACHE_DIR / f"{TAG.lower()}.json"

LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)

HTTP_SEMAPHORE = asyncio.Semaphore(10)

LIVE_LOGO = (
    "https://i.gyazo.com/"
    "4a5e9fa2525808ee4b65002b56d3450e.png"
)


LOG_FMT = (
    "[%(asctime)s] "
    "%(levelname)-8s "
    "[%(name)s] "
    "%(message)-70s "
    "(%(filename)s:%(lineno)d)"
)

log = logging.getLogger(TAG)
log.setLevel(logging.INFO)

if not log.handlers:
    formatter = logging.Formatter(
        LOG_FMT,
        datefmt="%Y-%m-%d | %H:%M:%S",
    )

    file_handler = logging.FileHandler(
        LOG_DIR / "fetch.log",
        encoding="utf-8",
    )

    console_handler = logging.StreamHandler()

    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    log.addHandler(file_handler)
    log.addHandler(console_handler)
    log.propagate = False


@dataclass(slots=True)
class Event:
    sport: str
    name: str
    link: str
    timestamp: float


def now_est() -> datetime:
    return datetime.now(
        ZoneInfo("America/New_York")
    ).replace(
        second=0,
        microsecond=0,
    )


def parse_event_time(value: str) -> datetime | None:
    try:
        value = value.strip()

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=ZoneInfo("America/New_York")
            )

        return dt

    except (ValueError, TypeError):
        return None


def cleanup(s: str) -> str:
    return "".join(
        i
        for i in s.split("—")[-1]
        if i.isascii()
    ).strip()


def fix_sport(s: str) -> str:
    splits = s.split()

    if not splits:
        return ""

    first = splits[0]

    return (
        f"{first.upper() if len(first) < 5 else first.capitalize()} "
        f"{' '.join(x.capitalize() for x in splits[1:])}"
    ).strip()


def cache_load() -> dict:
    try:
        data = json.loads(
            CACHE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (
        FileNotFoundError,
        json.JSONDecodeError,
    ):
        return {}

    if not isinstance(data, dict):
        return {}

    return {
        key: value
        for key, value in data.items()
        if isinstance(value, dict)
    }


def cache_write(data: dict) -> None:
    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    CACHE_FILE.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def get_tvg_info(
    sport: str,
    name: str,
) -> tuple[str | None, str]:
    return None, LIVE_LOGO


async def request(
    client: httpx.AsyncClient,
    url: str,
    url_num: int | None = None,
) -> httpx.Response | None:

    try:
        response = await client.get(url)

        response.raise_for_status()

        return response

    except httpx.TimeoutException as e:
        if url_num:
            log.error(
                f'URL {url_num}) Failed to fetch "{url}": {e}'
            )
        else:
            log.error(
                f'Failed to fetch "{url}": {e}'
            )

        return None

    except httpx.HTTPError as e:
        if url_num:
            log.error(
                f'URL {url_num}) Failed to fetch "{url}": {e}'
            )
        else:
            log.error(
                f'Failed to fetch "{url}": {e}'
            )

        return None


async def safe_process(
    fn,
    url_num: int,
    timeout: int | float = 30,
):
    async with HTTP_SEMAPHORE:
        task = asyncio.create_task(fn())

        try:
            return await asyncio.wait_for(
                task,
                timeout=timeout,
            )

        except asyncio.TimeoutError:
            log.warning(
                f"URL {url_num}) Timed out after "
                f"{timeout}s, skipping event"
            )

            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass

            return None

        except Exception as e:
            log.error(
                f"URL {url_num}) Unexpected error: {e}"
            )

            return None


async def process_event(
    client: httpx.AsyncClient,
    url: str,
    url_num: int,
) -> str | None:

    html_data = await request(
        client,
        url,
        url_num,
    )

    if not html_data:
        return None

    soup = HTMLParser(
        html_data.content
    )

    iframe = soup.css_first("iframe")

    if not iframe:
        log.warning(
            f"URL {url_num}) No iframe element found."
        )
        return None

    src = iframe.attributes.get("src")

    if not src:
        log.warning(
            f"URL {url_num}) iframe has no src."
        )
        return None

    splits = urlsplit(src)

    params = dict(
        parse_qsl(splits.query)
    )

    stream_id = params.get("stream")

    if not stream_id:
        log.warning(
            f"URL {url_num}) No stream ID found."
        )
        return None

    log.info(
        f"URL {url_num}) Captured M3U8"
    )

    if stream_id.isdigit():
        return (
            "https://edgestream3.pro/"
            f"stream/{stream_id}.m3u8"
        )

    return (
        "https://edgestream2.pro/"
        f"hls/{stream_id}.m3u8"
    )


async def get_events(
    client: httpx.AsyncClient,
) -> list[Event]:

    events: list[Event] = []

    html_data = await request(
        client,
        urljoin(
            BASE_URL,
            "game-cards/embed",
        ),
    )

    if not html_data:
        return events

    soup = HTMLParser(
        html_data.content
    )

    now = now_est()

    for card in soup.css(
        ".game-card-group"
    ):

        sport_elem = card.css_first("h2")

        if not sport_elem:
            continue

        sport = cleanup(
            sport_elem.text(strip=True)
        )

        for game in card.css(
            ".game-card-row"
        ):

            name_elem = game.css_first("h3")

            if not name_elem:
                continue

            event_time_elem = game.css_first(
                ".game-card-when > time"
            )

            if not event_time_elem:
                continue

            event_time = event_time_elem.attributes.get(
                "datetime"
            )

            if not event_time:
                continue

            event_dt = parse_event_time(
                event_time
            )

            if not event_dt:
                continue

            event_dt = event_dt.astimezone(
                now.tzinfo
            )

            if event_dt.date() != now.date():
                continue

            event_name = name_elem.text(
                strip=True
            )

            for source in game.css(
                ".game-card-source > "
                "a.game-card-open-link"
            ):

                href = source.attributes.get(
                    "href"
                )

                if not href:
                    continue

                lang = source.text(
                    strip=True
                )

                events.append(
                    Event(
                        sport=fix_sport(sport),
                        name=f"{event_name} | {lang}",
                        link=urljoin(
                            BASE_URL,
                            href,
                        ),
                        timestamp=now.timestamp(),
                    )
                )

    return events


async def scrape() -> None:

    cached_urls = cache_load()

    log.info(
        f'Scraping from "{BASE_URL}"'
    )

    async with httpx.AsyncClient(
        headers={
            "User-Agent": UA,
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,"
                "image/webp,"
                "*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        follow_redirects=True,
        timeout=5.0,
        http2=True,
    ) as client:

        events = await get_events(client)

        if not events:
            log.info("No events found")
            return

        log.info(
            f"Processing {len(events)} URL(s)"
        )

        current_urls = {}

        for i, ev in enumerate(
            events,
            start=1,
        ):

            async def handler(
                event_url=ev.link,
                event_num=i,
            ):
                return await process_event(
                    client,
                    event_url,
                    event_num,
                )

            source = await safe_process(
                handler,
                url_num=i,
                timeout=30,
            )

            key = (
                f"[{ev.sport}] "
                f"{ev.name} "
                f"({TAG})"
            )

            tvg_id, logo = get_tvg_info(
                ev.sport,
                ev.name,
            )

            old_entry = cached_urls.get(key)

            if source:
                entry = {
                    "source": source,
                    "logo": logo,
                    "refer": ALT_BASE,
                    "timestamp": ev.timestamp,
                    "tvg-id": tvg_id or "Live.Event.us",
                    "link": ev.link,
                }

                current_urls[key] = entry
                cached_urls[key] = entry

            elif old_entry:
                current_urls[key] = old_entry

        log.info(
            f"Collected {len(current_urls)} event(s)"
        )

    cache_write(cached_urls)


if __name__ == "__main__":
    asyncio.run(scrape())
