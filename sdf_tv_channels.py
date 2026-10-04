#!/usr/bin/env python3
"""SDF TV channel extractor, classifier and remote-source loader."""
from __future__ import annotations

import argparse
import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

ATTR_RE = re.compile(r'([\w-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s]*)')

CATEGORY_RULES = {
    "sports": ("sport", "sports", "futbol", "fútbol", "football", "soccer", "deporte",
               "espn", "fox sports", "bein", "sky sport", "dazn", "golf", "tennis", "nba"),
    "news": ("news", "noticias", "cnn", "bbc news", "al jazeera", "euronews",
             "24 horas", "franceinfo", "sky news", "reuters"),
    "movies": ("movie", "movies", "cinema", "cine", "pelicula", "película", "film",
               "hbo", "cinemax", "paramount movies", "film4"),
    "series": ("series", "drama", "comedy", "sitcom", "fiction", "novelas"),
    "kids": ("kids", "junior", "children", "infantil", "cartoon", "disney",
             "nickelodeon", "nick jr", "baby", "animation", "animación"),
    "documentary": ("documentary", "documental", "discovery", "history",
                    "nat geo", "national geographic", "smithsonian", "science"),
    "music": ("music", "musica", "música", "mtv", "vh1", "concert", "concierto"),
    "business": ("business", "financial", "finance", "economy", "economía", "markets"),
    "culture": ("culture", "cultura", "arts", "arte", "lifestyle"),
    "travel": ("travel", "viajes", "turismo", "tourism"),
    "cooking": ("cooking", "cook", "cocina", "food", "comida"),
    "religious": ("religious", "religion", "religión", "church", "iglesia", "gospel"),
    "weather": ("weather", "clima", "meteorologia", "meteorología"),
    "general": ("general", "entertainment", "variedades", "television", "tv"),
}

LANGUAGE_RULES = {
    "es": ("spanish", "español", "espanol", "latino", "latina", "castellano", "esp", "spa"),
    "en": ("english", "inglés", "ingles", "eng", "usa"),
    "pt": ("portuguese", "português", "portugues", "por", "brasil", "brazil"),
    "fr": ("french", "français", "francais", "fra"),
    "de": ("german", "deutsch", "ger"),
    "it": ("italian", "italiano", "ita"),
    "ja": ("japanese", "japonés", "japones", "jpn"),
    "ko": ("korean", "coreano", "kor"),
    "ar": ("arabic", "árabe", "arabe", "ara"),
}

COUNTRY_RULES = {
    "US": ("usa", "united states", "estados unidos", "us-"),
    "GB": ("united kingdom", "britain", "reino unido", "uk"),
    "ES": ("spain", "españa", "espana", "es-"),
    "MX": ("mexico", "méxico", "mx-"),
    "AR": ("argentina", "ar-"), "CO": ("colombia", "co-"),
    "CL": ("chile", "cl-"), "PE": ("peru", "perú", "pe-"),
    "BR": ("brazil", "brasil", "br-"), "CA": ("canada", "canadá", "ca-"),
    "FR": ("france", "fr-"), "DE": ("germany", "deutschland", "de-"),
    "IT": ("italy", "italia", "it-"), "JP": ("japan", "japón", "japon", "jp-"),
}

LANGUAGE_CODES = {
    "es": "es", "spa": "es", "en": "en", "eng": "en", "pt": "pt", "por": "pt",
    "fr": "fr", "fra": "fr", "de": "de", "ger": "de", "deu": "de",
    "it": "it", "ita": "it", "ja": "ja", "jpn": "ja", "ko": "ko", "kor": "ko",
    "ar": "ar", "ara": "ar",
}
COUNTRY_CODES = {
    x: x for x in (
        "US", "GB", "ES", "MX", "AR", "CO", "CL", "PE", "BR", "CA", "FR", "DE",
        "IT", "JP", "PT", "UY", "PY", "EC", "BO", "VE", "CR", "PA", "DO", "GT",
        "HN", "SV", "NI", "CU", "PR",
    )
}

@dataclass
class Channel:
    name: str
    url: str
    group: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    logo: str = ""
    language: str = "unknown"
    country: str = "unknown"
    category: str = "other"
    source: str = ""
    source_url: str = ""
    attributes: dict[str, str] | None = None

@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    url: str
    kind: str = "playlist"
    playlist_hints: tuple[str, ...] = ()
    page_prefixes: tuple[str, ...] = ()

DEFAULT_SOURCES = (
    SourceConfig("bbyte", "BByte Jellyfin", "https://iptv.bbyte.app/jellyfin/live.m3u"),
    SourceConfig("cxtv", "CXTv", "https://www.cxtvenvivo.com/", "site", page_prefixes=("/tv-en-vivo/",)),
    SourceConfig("tdtchannels", "TDTChannels", "https://www.tdtchannels.com/lists/tv.m3u8"),
    SourceConfig("m3ucl_top", "m3u.cl Top", "https://m3u.cl/lista/top.m3u"),
    SourceConfig("m3ucl_latam", "m3u.cl LATAM", "https://m3u.cl/lista/LATAM.m3u"),
    SourceConfig("iptv_org", "IPTV-org", "https://iptv-org.github.io/iptv/index.m3u"),
    SourceConfig("teleonline", "Teleonline", "https://teleonline.org/", "site",
                  playlist_hints=("https://teleonline.github.io/listas/tv.m3u8",),
                  page_prefixes=("/canal/",)),
)

USER_AGENT = "SDF-TV-Channel-Extractor/1.1"
M3U_URL_RE = re.compile(r'(?:(?:https?:)?//|/)[^<>"\'\s\\]+?\.m3u8?(?:\?[^<>"\'\s\\]*)?', re.I)
STREAM_URL_RE = re.compile(r'https?://[^<>"\'\s\\]+?(?:\.m3u8?|/hls/|/live/)[^<>"\'\s\\]*', re.I)

class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): v or "" for k, v in attrs}
        for key in ("href", "src", "data-src", "data-url", "data-stream", "data-hls", "playlist"):
            if a.get(key):
                self.links.append((a[key], ""))
        if tag.lower() == "a" and a.get("href"):
            self._href = a["href"]
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []

def _attributes(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in ATTR_RE.finditer(text):
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace(r'\"', '"').replace(r'\\', '\\')
        result[match.group(1).lower()] = value
    return result

def _match_label(text: str, rules: dict[str, tuple[str, ...]]) -> str:
    folded = text.casefold()
    for label, words in rules.items():
        if any(word.casefold() in folded for word in words):
            return label
    return "unknown"

def _code(value: str, mapping: dict[str, str], upper: bool = False) -> str:
    for token in re.split(r"[,;|/\s]+", value.strip()):
        token = token.upper() if upper else token.casefold()
        if token in mapping:
            return mapping[token]
    return ""

def classify_channel(name: str, group: str = "", attrs: dict[str, str] | None = None) -> tuple[str, str, str]:
    attrs = attrs or {}
    metadata = " ".join(
        (attrs.get("tvg-name", ""), attrs.get("tvg-id", ""),
         attrs.get("tvg-language", ""), attrs.get("tvg-country", ""))
    )
    category = _match_label(f"{name} {group}", CATEGORY_RULES)
    category = category if category != "unknown" else "other"
    language = _code(attrs.get("tvg-language", ""), LANGUAGE_CODES)
    language = language or _match_label(f"{name} {group} {metadata}", LANGUAGE_RULES)
    country = _code(attrs.get("tvg-country", ""), COUNTRY_CODES, upper=True)
    country = country or _match_label(f"{name} {group} {metadata}", COUNTRY_RULES)
    return category, language, country

def _header_parts(header: str) -> tuple[str, dict[str, str]]:
    quoted = escaped = False
    comma = -1
    for i, char in enumerate(header):
        if char == '"' and not escaped:
            quoted = not quoted
        if char == "," and not quoted:
            comma = i
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    metadata = header if comma < 0 else header[:comma]
    name = "" if comma < 0 else header[comma + 1:].strip()
    return name, _attributes(metadata)

def parse_m3u(text: str, *, deduplicate: bool = True, source: str = "") -> list[Channel]:
    result: list[Channel] = []
    pending: tuple[str, dict[str, str], str] | None = None
    seen: set[tuple[str, str]] = set()

    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            name, attrs = _header_parts(line[len("#EXTINF:"):])
            pending = (name or attrs.get("tvg-name", "") or "Unnamed channel",
                       attrs, attrs.get("group-title", ""))
            continue
        if line.startswith("#") or pending is None:
            continue

        name, attrs, group = pending
        parsed = urlparse(line)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "rtmp", "rtmps", "udp", "rtp", "rtsp", "file"}:
            pending = None
            continue
        if scheme in {"http", "https", "rtmp", "rtmps", "rtsp"} and not parsed.netloc:
            pending = None
            continue

        key = (name.casefold(), line)
        if not deduplicate or key not in seen:
            category, language, country = classify_channel(name, group, attrs)
            result.append(Channel(
                name=name, url=line, group=group,
                tvg_id=attrs.get("tvg-id", ""), tvg_name=attrs.get("tvg-name", ""),
                logo=attrs.get("tvg-logo", ""), language=language, country=country,
                category=category, source=source, attributes=attrs,
            ))
            seen.add(key)
        pending = None

    return result

def read_playlist(path: Path) -> list[Channel]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    return parse_m3u(text, source=str(path))

def _clean_url(value: str, base: str) -> str:
    value = unescape(value).strip().replace("\\/", "/").replace("\\u0026", "&")
    return urljoin(base, value) if value else ""

def _urls_from_html(html: str, base: str) -> list[str]:
    parser = LinkParser()
    parser.feed(html)
    raw = [v for v, _ in parser.links]
    raw += M3U_URL_RE.findall(html)
    raw += STREAM_URL_RE.findall(html)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        url = _clean_url(item, base)
        if urlparse(url).scheme.lower() not in {"http", "https"}:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out

def _page_title(html: str, fallback: str) -> str:
    for tag in ("title", "h1"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.I | re.S)
        if match:
            text = " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())
            if text:
                return text
    return fallback

def _fetch_text(url: str, timeout: int = 20, max_bytes: int = 25 * 1024 * 1024) -> str:
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/x-mpegURL, text/plain, text/html, */*",
    })
    with urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"response exceeds {max_bytes} bytes")
        encoding = response.headers.get_content_charset() or "utf-8"
    return data.decode(encoding, errors="replace")

def _looks_like_m3u(text: str) -> bool:
    sample = text.lstrip("\ufeff \r\n")
    return sample.startswith("#EXTM3U") or "#EXTINF:" in sample[:100000]

def _dedupe(channels: Iterable[Channel]) -> list[Channel]:
    out: list[Channel] = []
    index: dict[str, int] = {}
    for channel in channels:
        key = channel.url.casefold().strip()
        if not key:
            continue
        if key not in index:
            index[key] = len(out)
            out.append(channel)
            continue
        existing = out[index[key]]
        existing.source = ", ".join(dict.fromkeys(
            x.strip() for x in f"{existing.source},{channel.source}".split(",") if x.strip()
        ))
        if not existing.logo and channel.logo:
            existing.logo = channel.logo
        if existing.category == "other" and channel.category != "other":
            existing.category = channel.category
        if existing.language == "unknown" and channel.language != "unknown":
            existing.language = channel.language
        if existing.country == "unknown" and channel.country != "unknown":
            existing.country = channel.country
    return out

def _fetch_source(source: SourceConfig, timeout: int, max_pages: int) -> tuple[list[Channel], list[str]]:
    errors: list[str] = []
    try:
        initial = _fetch_text(source.url, timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return [], [f"{source.key}: {exc}"]

    if source.kind == "playlist" or _looks_like_m3u(initial):
        return parse_m3u(initial, source=source.name), []

    parser = LinkParser()
    parser.feed(initial)
    playlists = list(source.playlist_hints)
    playlists += [u for u in _urls_from_html(initial, source.url) if ".m3u" in u.lower()]

    channels: list[Channel] = []
    seen_playlists: set[str] = set()
    for playlist in playlists:
        if playlist in seen_playlists:
            continue
        seen_playlists.add(playlist)
        try:
            content = _fetch_text(playlist, timeout)
            if _looks_like_m3u(content):
                channels.extend(parse_m3u(content, source=source.name))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.key}: playlist {playlist}: {exc}")

    host = urlparse(source.url).netloc.lower()
    pages: list[tuple[str, str]] = []
    seen_pages: set[str] = {source.url}
    for href, label in parser.links:
        page = _clean_url(href, source.url)
        parsed = urlparse(page)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host:
            continue
        if page in seen_pages:
            continue
        if source.page_prefixes and not any(parsed.path.lower().startswith(x.lower()) for x in source.page_prefixes):
            continue
        seen_pages.add(page)
        pages.append((page, label))
        if len(pages) >= max_pages:
            break

    for page, label in pages:
        try:
            html = _fetch_text(page, timeout)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.key}: page {page}: {exc}")
            continue
        name = label or _page_title(html, source.name)
        for stream in _urls_from_html(html, page):
            if ".m3u8" not in stream.lower() and ".m3u" not in stream.lower():
                continue
            category, language, country = classify_channel(name, source.name)
            channels.append(Channel(
                name=name, url=stream, group=source.name,
                language=language, country=country, category=category,
                source=source.name, source_url=page,
            ))
    return _dedupe(channels), errors

def fetch_sources(sources: Iterable[SourceConfig], timeout: int = 20,
                  workers: int = 4, max_pages: int = 30) -> tuple[list[Channel], dict[str, list[str]]]:
    source_list = list(sources)
    channels: list[Channel] = []
    errors: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_fetch_source, source, timeout, max_pages): source for source in source_list
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                items, source_errors = future.result()
            except Exception as exc:
                items, source_errors = [], [f"{source.key}: unexpected error: {exc}"]
            channels.extend(items)
            if source_errors:
                errors[source.key] = source_errors
    channels = _dedupe(channels)
    channels.sort(key=lambda x: (x.category, x.language, x.name.casefold(), x.url))
    return channels, errors

def write_output(channels: Iterable[Channel], output: Path, fmt: str) -> None:
    rows = [asdict(c) for c in channels]
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    fields = ["name", "url", "group", "tvg_id", "tvg_name", "logo",
              "language", "country", "category", "source", "source_url"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract and classify TV channels from local playlists or configured public sources."
    )
    parser.add_argument("input", nargs="*", type=Path, help="Local .m3u/.m3u8 files")
    parser.add_argument("--source", action="append",
                        choices=[s.key for s in DEFAULT_SOURCES],
                        help="Remote source key; repeat for several sources")
    parser.add_argument("--all-sources", action="store_true")
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument("-o", "--output", type=Path, default=Path("channels.json"))
    parser.add_argument("--format", choices=("json", "csv"), default=None)
    parser.add_argument("--category")
    parser.add_argument("--language")
    parser.add_argument("--country")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=30)
    args = parser.parse_args(argv)

    if args.list_sources:
        for source in DEFAULT_SOURCES:
            print(f"{source.key}\t{source.name}\t{source.url}")
        return 0

    channels: list[Channel] = []
    for path in args.input:
        if not path.is_file():
            parser.error(f"input file does not exist: {path}")
        channels.extend(read_playlist(path))

    if args.all_sources:
        selected = DEFAULT_SOURCES
    elif args.source:
        keys = set(args.source)
        selected = tuple(s for s in DEFAULT_SOURCES if s.key in keys)
    else:
        selected = ()

    if selected:
        remote_channels, errors = fetch_sources(
            selected, timeout=max(1, args.timeout),
            workers=max(1, args.workers), max_pages=max(0, args.max_pages),
        )
        channels.extend(remote_channels)
    else:
        errors = {}

    channels = _dedupe(channels)
    if args.category:
        channels = [c for c in channels if c.category == args.category.casefold()]
    if args.language:
        channels = [c for c in channels if c.language == args.language.casefold()]
    if args.country:
        channels = [c for c in channels if c.country == args.country.upper()]
    channels.sort(key=lambda x: (x.category, x.language, x.name.casefold(), x.url))

    fmt = args.format or ("csv" if args.output.suffix.lower() == ".csv" else "json")
    write_output(channels, args.output, fmt)
    print(f"Exported {len(channels)} channels to {args.output}")

    for source_key, source_errors in sorted(errors.items()):
        for error in source_errors[:10]:
            print(f"WARNING: {error}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
