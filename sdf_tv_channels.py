#!/usr/bin/env python3
"""SDF TV channel extractor, classifier and remote-source loader."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ATTR_RE = re.compile(r'([\w-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s]*)')

CATEGORY_RULES = {
    "sports": ("sport", "sports", "futbol", "football", "soccer", "deporte", "deportes",
               "espn", "fox sports", "bein", "sky sport", "dazn", "golf", "tennis", "nba",
               "basketball", "baseball", "hockey", "rugby", "cricket", "formula 1", "formula one",
               "f1", "ufc", "boxing", "volleyball", "wrestling", "motorsport"),
    "news": ("news", "noticias", "cnn", "bbc news", "al jazeera", "euronews",
             "24 horas", "franceinfo", "sky news", "fox news", "abc news", "nbc news",
             "msnbc", "reuters"),
    "movies": ("movie", "movies", "cinema", "cine", "pelicula", "peliculas", "film",
               "hbo", "cinemax", "paramount movies", "film4"),
    "series": ("series", "drama", "dramas", "comedy", "comedies", "sitcom", "fiction", "novelas"),
    "kids": ("kids", "junior", "children", "infantil", "cartoon", "disney",
             "nickelodeon", "nick jr", "baby", "animation", "animacion", "cartoons"),
    "documentary": ("documentary", "documental", "documentales", "discovery", "history",
                    "nat geo", "national geographic", "smithsonian", "science", "bbc earth",
                    "animal planet"),
    "music": ("music", "musica", "mtv", "vh1", "concert", "concierto", "conciertos"),
    "business": ("business", "financial", "finance", "economy", "economia", "markets",
                 "cnbc", "bloomberg"),
    "culture": ("culture", "cultura", "arts", "arte", "lifestyle"),
    "travel": ("travel", "viajes", "turismo", "tourism"),
    "cooking": ("cooking", "cook", "cocina", "food", "comida", "gastronomia"),
    "religious": ("religious", "religion", "church", "iglesia", "gospel"),
    "weather": ("weather", "clima", "meteorologia"),
    "general": ("general", "entertainment", "variedades", "variety"),
}

LANGUAGE_RULES = {
    "es": ("spanish", "espanol", "latino", "latina", "castellano", "spa", "deportes"),
    "en": ("english", "ingles", "eng"),
    "pt": ("portuguese", "portugues", "por"),
    "fr": ("french", "francais", "fra"),
    "de": ("german", "deutsch", "ger"),
    "it": ("italian", "italiano", "ita"),
    "ja": ("japanese", "japones", "jpn"),
    "ko": ("korean", "coreano", "kor"),
    "ar": ("arabic", "arabe", "ara"),
    "zh": ("chinese", "mandarin", "chino", "zho", "chi"),
    "ru": ("russian", "ruso", "rus"),
    "hi": ("hindi", "hin"),
    "tr": ("turkish", "turco", "tur"),
    "nl": ("dutch", "nederlands", "holandes"),
    "el": ("greek", "griego", "ell", "gre"),
    "pl": ("polish", "polaco", "pol"),
    "sv": ("swedish", "sueco", "swe"),
    "no": ("norwegian", "noruego", "nor"),
    "da": ("danish", "danes", "dan"),
    "fi": ("finnish", "finlandes", "fin"),
    "he": ("hebrew", "hebreo", "heb"),
    "th": ("thai", "tailandes", "tha"),
    "vi": ("vietnamese", "vietnamita", "vie"),
    "id": ("indonesian", "bahasa", "ind"),
    "uk": ("ukrainian", "ucraniano", "ukr"),
}

COUNTRY_RULES = {
    "US": ("usa", "united states", "estados unidos"),
    "GB": ("united kingdom", "great britain", "britain", "reino unido"),
    "ES": ("spain", "espana"),
    "MX": ("mexico",),
    "AR": ("argentina",), "CO": ("colombia",), "CL": ("chile",),
    "PE": ("peru",), "BR": ("brazil", "brasil"), "CA": ("canada",),
    "FR": ("france",), "DE": ("germany", "deutschland"),
    "IT": ("italy", "italia"), "JP": ("japan",), "PT": ("portugal",),
    "UY": ("uruguay",), "PY": ("paraguay",), "EC": ("ecuador",),
    "BO": ("bolivia",), "VE": ("venezuela",), "CR": ("costa rica",),
    "PA": ("panama",), "DO": ("dominican republic", "republica dominicana"),
    "GT": ("guatemala",), "HN": ("honduras",), "SV": ("el salvador",),
    "NI": ("nicaragua",), "CU": ("cuba",), "PR": ("puerto rico",),
    "AU": ("australia",), "NZ": ("new zealand", "nueva zelanda"),
    "IE": ("ireland", "irlanda"), "NL": ("netherlands", "holland", "paises bajos"),
    "BE": ("belgium", "belgica"), "CH": ("switzerland", "suiza"),
    "AT": ("austria",), "SE": ("sweden", "suecia"), "NO": ("norway", "noruega"),
    "DK": ("denmark", "dinamarca"), "FI": ("finland", "finlandia"),
    "GR": ("greece", "grecia"), "PL": ("poland", "polonia"),
    "RU": ("russia", "rusia"), "CN": ("china",), "IN": ("india",),
    "TR": ("turkey", "turquia"), "KR": ("south korea", "corea del sur"),
    "ZA": ("south africa", "sudafrica"),
}

LANGUAGE_CODES = {
    "es": "es", "spa": "es", "en": "en", "eng": "en", "pt": "pt", "por": "pt",
    "fr": "fr", "fra": "fr", "de": "de", "ger": "de", "deu": "de",
    "it": "it", "ita": "it", "ja": "ja", "jpn": "ja", "ko": "ko", "kor": "ko",
    "ar": "ar", "ara": "ar", "zh": "zh", "zho": "zh", "chi": "zh",
    "ru": "ru", "rus": "ru", "hi": "hi", "hin": "hi", "tr": "tr", "tur": "tr",
    "nl": "nl", "nld": "nl", "dut": "nl", "el": "el", "ell": "el", "gre": "el",
    "pl": "pl", "pol": "pl", "sv": "sv", "swe": "sv", "no": "no", "nor": "no",
    "da": "da", "dan": "da", "fi": "fi", "fin": "fi", "he": "he", "heb": "he",
    "th": "th", "tha": "th", "vi": "vi", "vie": "vi", "id": "id", "ind": "id",
    "uk": "uk", "ukr": "uk",
}
COUNTRY_CODES = {
    x: x for x in (
        "US", "GB", "ES", "MX", "AR", "CO", "CL", "PE", "BR", "CA", "FR", "DE",
        "IT", "JP", "PT", "UY", "PY", "EC", "BO", "VE", "CR", "PA", "DO", "GT",
        "HN", "SV", "NI", "CU", "PR", "AU", "NZ", "IE", "NL", "BE", "CH", "AT",
        "SE", "NO", "DK", "FI", "GR", "PL", "RU", "CN", "IN", "TR", "KR", "ZA",
    )
}
COUNTRY_CODES.update({
    "USA": "US", "GBR": "GB", "UK": "GB", "ESP": "ES", "MEX": "MX",
    "ARG": "AR", "COL": "CO", "CHL": "CL", "PER": "PE", "BRA": "BR",
    "CAN": "CA", "FRA": "FR", "DEU": "DE", "GER": "DE", "ITA": "IT",
    "JPN": "JP", "PRT": "PT", "URY": "UY", "PRY": "PY", "ECU": "EC",
    "BOL": "BO", "VEN": "VE", "CRI": "CR", "PAN": "PA", "DOM": "DO",
    "GTM": "GT", "HND": "HN", "SLV": "SV", "NIC": "NI", "CUB": "CU",
    "PRI": "PR", "AUS": "AU", "NZL": "NZ", "IRL": "IE", "NLD": "NL",
    "BEL": "BE", "CHE": "CH", "AUT": "AT", "SWE": "SE", "NOR": "NO",
    "DNK": "DK", "FIN": "FI", "GRC": "GR", "POL": "PL", "RUS": "RU",
    "CHN": "CN", "IND": "IN", "TUR": "TR", "KOR": "KR", "ZAF": "ZA",
})

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
class StreamCheck:
    """A bounded HTTP reachability check; it does not guarantee playback."""

    status: str
    http_status: int | None = None
    content_type: str = ""
    final_url: str = ""
    detail: str = ""


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

USER_AGENT = "SDF-TV-Channel-Extractor/1.2"
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

def _fold_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Compile word-boundary matchers once, avoiding substring false positives."""
    words = _fold_text(term).split()
    pattern = r"[\W_]+".join(re.escape(word) for word in words)
    return re.compile(rf"(?<!\w){pattern}(?!\w)")


def _match_label(text: str, rules: dict[str, tuple[str, ...]]) -> str:
    folded = _fold_text(text)
    for label, words in rules.items():
        if any(_term_pattern(word).search(folded) for word in words):
            return label
    return "unknown"


def _code(value: str, mapping: dict[str, str], upper: bool = False) -> str:
    for token in re.split(r"[,;|/\s]+", value.strip()):
        candidates = [token]
        if "-" in token or "_" in token:
            candidates.append(re.split(r"[-_]", token, maxsplit=1)[0])
        for candidate in candidates:
            candidate = candidate.upper() if upper else candidate.casefold()
            if candidate in mapping:
                return mapping[candidate]
    return ""


def _country_from_tvg_id(tvg_id: str) -> str:
    match = re.search(r"[._-]([a-z]{2,3})$", tvg_id.strip(), re.I)
    return _code(match.group(1), COUNTRY_CODES, upper=True) if match else ""


def classify_channel(name: str, group: str = "",
                     attrs: dict[str, str] | None = None) -> tuple[str, str, str]:
    attrs = {key.casefold(): value for key, value in (attrs or {}).items()}
    tvg_name = attrs.get("tvg-name", "")
    tvg_id = attrs.get("tvg-id", "")
    language_value = attrs.get("tvg-language", "")
    country_value = attrs.get("tvg-country", "")

    category = _match_label(f"{name} {group} {tvg_name}", CATEGORY_RULES)
    category = category if category != "unknown" else "other"

    language = _code(language_value, LANGUAGE_CODES)
    if not language:
        language = _match_label(language_value, LANGUAGE_RULES)
    if language == "unknown":
        language = _match_label(f"{name} {group} {tvg_name}", LANGUAGE_RULES)

    country = _code(country_value, COUNTRY_CODES, upper=True)
    if not country:
        country = _match_label(country_value, COUNTRY_RULES)
    if country == "unknown":
        country = _country_from_tvg_id(tvg_id)
    if not country:
        country = _match_label(f"{name} {group} {tvg_name}", COUNTRY_RULES)

    return category, language, country

def _header_parts(header: str) -> tuple[str, dict[str, str]]:
    quoted = escaped = False
    comma = -1
    for i, char in enumerate(header):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            comma = i
            break
    metadata = header if comma < 0 else header[:comma]
    name = "" if comma < 0 else header[comma + 1:].strip()
    return name, _attributes(metadata)


def parse_m3u(text: str, *, deduplicate: bool = True, source: str = "",
              base_url: str = "") -> list[Channel]:
    lines = text.lstrip("\ufeff").splitlines()
    # A media playlist describes segments of one stream, not a list of TV channels.
    upper_lines = [line.strip().upper() for line in lines[:100]]
    if any(line.startswith(("#EXT-X-TARGETDURATION:", "#EXT-X-MEDIA-SEQUENCE:"))
           for line in upper_lines):
        return []

    result: list[Channel] = []
    pending: tuple[str, dict[str, str], str] | None = None
    seen: set[tuple[str, str]] = set()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            name, attrs = _header_parts(line[len("#EXTINF:"):])
            pending = (name or attrs.get("tvg-name", "") or "Unnamed channel",
                       attrs, attrs.get("group-title", ""))
            continue
        if line.upper().startswith("#EXTGRP:") and pending is not None:
            name, attrs, _ = pending
            pending = (name, attrs, line.split(":", 1)[1].strip())
            continue
        if line.startswith("#") or pending is None:
            continue

        name, attrs, group = pending
        if base_url and not urlparse(line).scheme:
            line = urljoin(base_url, line)
        parsed = urlparse(line)
        scheme = parsed.scheme.lower()
        if scheme not in {
            "http", "https", "rtmp", "rtmpe", "rtmps", "rtmpt", "rtmpts",
            "udp", "rtp", "rtsp", "mms", "mmsh", "mmst", "srt", "ftp", "file",
        }:
            pending = None
            continue
        if (
            scheme in {"http", "https", "ftp", "rtmp", "rtmpe", "rtmps", "rtmpt", "rtmpts", "rtsp"}
            and not parsed.netloc
        ):
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
    return parse_m3u(text, source=str(path), base_url=path.resolve().as_uri())

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
            text = " ".join(unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())
            if text:
                return text
    return fallback

def _page_logo(html: str, base_url: str) -> str:
    """Extract a page logo without executing JavaScript."""
    match = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if not match:
        match = re.search(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)', html, re.I)
    return urljoin(base_url, unescape(match.group(1))) if match else ""

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


def check_stream(url: str, timeout: int = 10) -> StreamCheck:
    """Check a small HTTP byte range; never download a full live stream."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return StreamCheck("unsupported", detail="Only absolute HTTP(S) URLs can be checked")

    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
        "Range": "bytes=0-4095",
    })
    try:
        with urlopen(request, timeout=max(1, timeout)) as response:
            status = int(response.status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            final_url = response.geturl()
            sample = response.read(4096)
    except HTTPError as exc:
        content_type = exc.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        state = "restricted" if exc.code in {401, 403, 407, 451} else "http_error"
        try:
            final_url = exc.geturl()
        except AttributeError:
            final_url = url
        exc.close()
        return StreamCheck(state, exc.code, content_type, final_url, f"HTTP {exc.code}")
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return StreamCheck("unreachable", detail=str(reason)[:240])

    if not 200 <= status < 300:
        return StreamCheck("http_error", status, content_type, final_url, f"HTTP {status}")

    playlist_types = {
        "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
        "audio/x-mpegurl",
    }
    is_playlist = (
        urlparse(final_url).path.lower().endswith((".m3u", ".m3u8"))
        or content_type in playlist_types
    )
    if is_playlist and not sample.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U"):
        return StreamCheck(
            "invalid_playlist", status, content_type, final_url,
            "The endpoint responded, but no #EXTM3U playlist header was found",
        )
    detail = (
        "Playlist header detected; segments were not tested"
        if is_playlist else "HTTP response received; playback was not tested"
    )
    return StreamCheck("http_ok", status, content_type, final_url, detail)


def check_streams(channels: Iterable[Channel], timeout: int = 10,
                  workers: int = 4) -> dict[str, StreamCheck]:
    urls = list(dict.fromkeys(channel.url for channel in channels if channel.url))
    if not urls:
        return {}

    results: dict[str, StreamCheck] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls)))) as pool:
        futures = {pool.submit(check_stream, url, timeout): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as exc:
                results[url] = StreamCheck("error", detail=str(exc)[:240])
    return results


def _url_key(value: str) -> str:
    """Normalize only URL components that are case-insensitive (not path/query)."""
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return raw
        port = parsed.port
    except ValueError:
        return raw

    credentials = parsed.netloc.rpartition("@")[0] + "@" if "@" in parsed.netloc else ""
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    netloc = credentials + host + (f":{port}" if port and port != default_port else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def _dedupe(channels: Iterable[Channel]) -> list[Channel]:
    out: list[Channel] = []
    index: dict[str, int] = {}
    for channel in channels:
        key = _url_key(channel.url)
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
        for field in ("group", "tvg_id", "tvg_name", "logo", "source_url"):
            if not getattr(existing, field) and getattr(channel, field):
                setattr(existing, field, getattr(channel, field))
        if existing.category == "other" and channel.category != "other":
            existing.category = channel.category
        if existing.language == "unknown" and channel.language != "unknown":
            existing.language = channel.language
        if existing.country == "unknown" and channel.country != "unknown":
            existing.country = channel.country
        if channel.attributes:
            if existing.attributes is None:
                existing.attributes = dict(channel.attributes)
            else:
                for key_name, value in channel.attributes.items():
                    if value and not existing.attributes.get(key_name):
                        existing.attributes[key_name] = value
    return out

def _fetch_source(source: SourceConfig, timeout: int,
                  max_pages: int) -> tuple[list[Channel], list[str]]:
    errors: list[str] = []
    try:
        initial = _fetch_text(source.url, timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return [], [f"{source.key}: {exc}"]

    if source.kind == "playlist" or _looks_like_m3u(initial):
        return parse_m3u(initial, source=source.name, base_url=source.url), []

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
                channels.extend(parse_m3u(content, source=source.name, base_url=playlist))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.key}: playlist {playlist}: {exc}")

    host = urlparse(source.url).netloc.lower()
    pages: list[tuple[str, str]] = []
    seen_pages: set[str] = {source.url}
    for href, label in parser.links:
        if len(pages) >= max_pages:
            break
        page = _clean_url(href, source.url)
        parsed = urlparse(page)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host:
            continue
        if page in seen_pages:
            continue
        if source.page_prefixes and not any(
            parsed.path.lower().startswith(prefix.lower()) for prefix in source.page_prefixes
        ):
            continue
        seen_pages.add(page)
        pages.append((page, label))

    for page, label in pages:
        try:
            html = _fetch_text(page, timeout)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.key}: page {page}: {exc}")
            continue
        name = label or _page_title(html, source.name)
        logo = _page_logo(html, page)
        for stream in _urls_from_html(html, page):
            if ".m3u8" not in stream.lower() and ".m3u" not in stream.lower():
                continue
            category, language, country = classify_channel(name, source.name)
            channels.append(Channel(
                name=name, url=stream, group=source.name, logo=logo,
                language=language, country=country, category=category,
                source=source.name, source_url=page,
            ))
    return _dedupe(channels), errors

def fetch_sources(
    sources: Iterable[SourceConfig], timeout: int = 20,
    workers: int = 4, max_pages: int = 30,
) -> tuple[list[Channel], dict[str, list[str]]]:
    source_list = list(sources)
    if not source_list:
        return [], {}

    results: list[tuple[list[Channel], list[str]]] = [([], []) for _ in source_list]
    worker_count = max(1, min(workers, len(source_list)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(_fetch_source, source, timeout, max_pages): (index, source)
            for index, source in enumerate(source_list)
        }
        for future in as_completed(futures):
            index, source = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = ([], [f"{source.key}: unexpected error: {exc}"])

    channels: list[Channel] = []
    errors: dict[str, list[str]] = {}
    for source, (items, source_errors) in zip(source_list, results):
        channels.extend(items)
        if source_errors:
            errors[source.key] = source_errors
    channels = _dedupe(channels)
    channels.sort(key=lambda x: (x.category, x.language, x.name.casefold(), x.url))
    return channels, errors

def write_output(channels: Iterable[Channel], output: Path, fmt: str,
                 stream_checks: dict[str, StreamCheck] | None = None) -> None:
    channel_list = list(channels)
    rows = [asdict(channel) for channel in channel_list]
    if stream_checks is not None:
        for channel, row in zip(channel_list, rows):
            check = stream_checks.get(channel.url, StreamCheck("not_checked"))
            row["stream_check"] = asdict(check)

    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return

    fields = ["name", "url", "group", "tvg_id", "tvg_name", "logo",
              "language", "country", "category", "source", "source_url"]
    csv_rows = rows
    if stream_checks is not None:
        fields.extend(("stream_status", "stream_http_status", "stream_content_type",
                       "stream_final_url", "stream_detail"))
        csv_rows = []
        for channel, row in zip(channel_list, rows):
            check = stream_checks.get(channel.url, StreamCheck("not_checked"))
            csv_row = dict(row)
            csv_row.update({
                "stream_status": check.status,
                "stream_http_status": check.http_status,
                "stream_content_type": check.content_type,
                "stream_final_url": check.final_url,
                "stream_detail": check.detail,
            })
            csv_rows.append(csv_row)

    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_rows)

def sync_to_supabase(channels: Iterable[Channel], url: str, key: str,
                     stream_checks: dict[str, StreamCheck] | None = None,
                     activation_mode: str = "manual", default_country: str = "US") -> int:
    """Upsert channels into public.tv_channels using Supabase REST.

    The service-role key is read from the environment and is never written to output.
    In automatic mode only streams returning HTTP 2xx are activated.
    """
    # Deliberadamente fijo: esta integración solo escribe en public.tv_channels,
    # nunca en tv_channel, channels u otra tabla del proyecto.
    endpoint = url.rstrip("/") + "/rest/v1/tv_channels?on_conflict=slug"
    rows = []
    for channel in channels:
        country = (channel.country if len(channel.country) == 2 else default_country).upper()
        slug = re.sub(r"[^a-z0-9]+", "-", channel.name.casefold()).strip("-")
        if not slug or not channel.url:
            continue
        check = stream_checks.get(channel.url) if stream_checks else None
        active = True if activation_mode == "manual" else bool(check and check.status == "http_ok")
        rows.append({"name": channel.name, "slug": slug, "logo_url": channel.logo or None,
                     "stream_url": channel.url, "stream_type": "hls",
                     "category": channel.category, "country_code": country,
                     "is_active": active})
    if not rows:
        return 0
    request = Request(endpoint, data=json.dumps(rows, ensure_ascii=False).encode(), method="POST",
                      headers={"apikey": key, "Authorization": f"Bearer {key}",
                               "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        with urlopen(request, timeout=30) as response:
            if response.status >= 300:
                raise RuntimeError(f"Supabase HTTP {response.status}")
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"No se pudo sincronizar con Supabase: {exc}") from exc
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract and classify TV channels from local playlists or configured public sources."
        )
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
    parser.add_argument("--check-streams", action="store_true",
                        help="Probe a small HTTP byte range and include reachability results")
    parser.add_argument("--sync-supabase", action="store_true",
                        help="Upsert the result into public.tv_channels")
    parser.add_argument("--activation-mode", choices=("manual", "automatic"), default="manual",
                        help="manual activates imported channels; automatic activates only HTTP-ok streams")
    parser.add_argument("--supabase-country", default="US",
                        help="Country fallback when the channel country is unknown (must exist in countries)")
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

    stream_checks = None
    if args.check_streams:
        stream_checks = check_streams(
            channels, timeout=max(1, args.timeout), workers=max(1, args.workers),
        )
        summary = Counter(check.status for check in stream_checks.values())
        counts = ", ".join(f"{status}={count}" for status, count in sorted(summary.items()))
        print(f"Checked {len(stream_checks)} stream URLs: {counts or 'none'}")

    fmt = args.format or ("csv" if args.output.suffix.lower() == ".csv" else "json")
    write_output(channels, args.output, fmt, stream_checks=stream_checks)
    print(f"Exported {len(channels)} channels to {args.output}")

    if args.sync_supabase:
        # Se aceptan los nombres habituales y las variantes usadas por el proyecto.
        supabase_url = (os.environ.get("SUPABASE_URL") or os.environ.get("Supabase_URL")
                        or os.environ.get("supabase_url"))
        supabase_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                        or os.environ.get("SUPABASE_SECRET_KEY")
                        or os.environ.get("SUPABASE_KEY")
                        or os.environ.get("Secret_Key")
                        or os.environ.get("SUPABASE_SECRET"))
        if not supabase_url or not supabase_key:
            parser.error("--sync-supabase requiere SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SECRET_KEY)")
        if args.activation_mode == "automatic" and stream_checks is None:
            parser.error("--activation-mode automatic requiere también --check-streams")
        try:
            synced = sync_to_supabase(channels, supabase_url, supabase_key, stream_checks,
                                      args.activation_mode, args.supabase_country)
        except RuntimeError as exc:
            parser.error(str(exc))
        print(f"Sincronizados {synced} canales en Supabase ({args.activation_mode})")

    for source_key, source_errors in sorted(errors.items()):
        for error in source_errors[:10]:
            print(f"WARNING: {error}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
