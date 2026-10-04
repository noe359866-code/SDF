#!/usr/bin/env python3
"""SDF TV channel extractor, classifier and remote-source loader."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from functools import lru_cache
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

DEFAULT_MAX_CHANNELS = 20
DEFAULT_MAX_STREAM_PROBES = 120

ATTR_RE = re.compile(r'([\w-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s]*)')

CATEGORY_RULES = {
    "sports": ("sport", "sports", "futbol", "football", "soccer", "deporte", "deportes",
               "espn", "fox sports", "bein", "sky sport", "dazn", "golf", "tennis", "nba",
               "basketball", "baseball", "hockey", "rugby", "cricket", "formula 1", "formula one",
               "f1", "ufc", "boxing", "volleyball", "wrestling", "motorsport", "tyc sports",
               "tudn", "win sports", "gol tv", "goltv", "teledeporte", "directv sports",
               "ovacion", "afizzionados", "claro sports"),
    "news": ("news", "noticias", "cnn", "bbc news", "al jazeera", "euronews",
             "24 horas", "franceinfo", "sky news", "fox news", "abc news", "nbc news",
             "msnbc", "reuters", "dw", "rt", "telesur", "ntn24", "adn 40", "milenio",
             "todo noticias", "c5n", "canal n", "24h", "informativo", "informativos",
             "france 24", "cbc news", "telediario", "noticiero"),
    "movies": ("movie", "movies", "cinema", "cine", "pelicula", "peliculas", "film",
               "hbo", "cinemax", "paramount movies", "film4", "tcm", "space", "golden",
               "de pelicula", "studio universal", "cinecanal", "amc", "fxm", "syfy",
               "star action", "multipremier", "cinelatino", "dark", "somos"),
    "series": ("series", "drama", "dramas", "comedy", "comedies", "sitcom", "fiction", "novelas",
               "telenovela", "telenovelas", "warner", "sony channel", "universal tv", "axn",
               "tnt series", "atreseries", "las estrellas", "tlc", "pasiones", "tlnovelas",
               "distrito comedia", "comedy central", "factoria de ficcion", "fdf", "neox", "nova"),
    "kids": ("kids", "junior", "children", "infantil", "cartoon", "disney",
             "nickelodeon", "nick jr", "baby", "animation", "animacion", "cartoons",
             "clan", "boing", "discovery kids", "cartoonito", "pakapaka", "tooncast", "anime",
             "bitme", "semillitas", "babyfirst", "dreamworks"),
    "documentary": ("documentary", "documental", "documentales", "discovery", "history",
                    "nat geo", "national geographic", "smithsonian", "science", "bbc earth",
                    "animal planet", "odisea", "docu", "historia", "natgeo", "crimen",
                    "investigation", "dmax", "be mad", "mega"),
    "music": ("music", "musica", "mtv", "vh1", "concert", "concierto", "conciertos",
              "telehit", "htv", "hit tv", "kiss tv", "mezzo", "bandamax", "ritmoson",
              "quiero musica", "vmusica", "stingray", "trace"),
    "business": ("business", "financial", "finance", "economy", "economia", "markets",
                 "cnbc", "bloomberg", "negocios", "finanzas", "intereconomia"),
    "culture": ("culture", "cultura", "arts", "arte", "lifestyle", "educativo", "cultural",
                "encuentro", "canal 22", "once", "tv unam", "senal colombia", "ciudad magazine",
                "la 2", "33", "ingenio"),
    "travel": ("travel", "viajes", "turismo", "tourism", "sun channel", "intriper", "hola tv"),
    "cooking": ("cooking", "cook", "cocina", "food", "comida", "gastronomia", "el gourmet",
                "canal cocina", "food network", "gusto tv"),
    "religious": ("religious", "religion", "church", "iglesia", "gospel", "ewtn", "enlace",
                  "catolico", "cristiano", "bethel", "13c", "cristovision", "trece", "orbe 21"),
    "weather": ("weather", "clima", "meteorologia", "accuweather", "weather channel", "eltiempo"),
    "general": ("general", "entertainment", "variedades", "variety", "entretenimiento",
                "generalista", "nacional", "abierta", "antena 3", "telecinco", "cuatro",
                "la sexta", "la 1", "telefe", "el trece", "america tv", "azteca uno",
                "imagen television", "caracol", "rcn", "chilevision", "mega", "tvn", "latina",
                "panamericana", "univision", "telemundo"),
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

CATEGORY_PRIORITY = (
    "general", "news", "sports", "movies", "series",
    "documentary", "kids", "music", "culture", "cooking", "travel",
    "business", "weather", "religious", "other",
)

NOISE_BRACKET_RE = re.compile(
    r"[\[(]\s*(?:"
    r"\d{3,4}[pi]|4k|8k|uhd|fhd|hd|sd|hevc|h\.?26[45]|50\s*fps|60\s*fps|"
    r"geo[- ]?blocked|geo|not\s*24/7|24/7|backup|opc(?:ion)?\s*\d+|server\s*\d+|"
    r"se[ñn]al\s*\d+|feed\s*\d+|en\s*vivo|live|online|gratis|multi[- ]?audio|"
    r"[+-]?\d+\s*h(?:oras?|rs?)?|hls|m3u8?|opcional|alternativo"
    r")[^])\n]*[\])]",
    re.I,
)
LEADING_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*[-.):|]\s*(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ\[\(])")
COUNTRY_PREFIX_RE = re.compile(
    r"^\s*(?:\[([A-Za-z]{2,3})\]|\(([A-Za-z]{2,3})\)|([A-Za-z]{2,3})\s*[:|/-])\s*",
)
SEO_PREFIX_RE = re.compile(
    r"^\s*(?:ver|watch|mirar)\s+(.+?)(?=\s+(?:en\s+vivo|en\s+directo|online|live|gratis)\b|$)",
    re.I,
)
SEO_SUFFIX_RE = re.compile(
    r"\s*(?:[-|–—:]\s*(?:cxtv(?:\s*en\s*vivo)?|teleonline|tv\s*en\s*vivo|en\s*vivo|en\s*directo|online\s*gratis).*|"
    r"\b(?:en\s+vivo(?:\s+online)?(?:\s+gratis)?|en\s+directo|online\s+gratis|live\s+stream(?:ing)?)\s*)$",
    re.I,
)
VARIANT_TAIL_TOKENS = {
    "hd", "fhd", "uhd", "4k", "8k", "sd", "1080p", "1080i", "720p", "576p",
    "480p", "360p", "hevc", "h264", "h265", "50fps", "60fps", "live",
    "online", "stream", "streaming", "backup", "mirror", "gratis",
    "latam", "latinoamerica", "sur", "norte", "este", "oeste", "east", "west",
    "internacional", "international", "int", "hls", "m3u8", "oficial",
}
CHANNEL_WRAPPER_TOKENS = {"tv", "television", "channel", "canal"}
NUMBERED_TAIL_PARENTS = {"senal", "signal", "opc", "opcion", "option", "server", "feed", "fuente"}
GENERIC_CHANNEL_NAMES = {
    "", "unnamed", "unnamed-channel", "channel", "canal", "tv", "live",
    "stream", "cxtv", "teleonline", "m3u-cl", "tdtchannels", "iptv-org",
    "bbyte", "bbyte-jellyfin", "unknown", "test", "playlist",
}
INVALID_LOGO_VALUES = {"", "n/a", "na", "null", "none", "undefined", "false", "0"}


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

USER_AGENT = "SDF-TV-Channel-Extractor/1.3"
M3U_URL_RE = re.compile(r'(?:(?:https?:)?//|/)[^<>"\'\s\\]+?\.m3u8?(?:\?[^<>"\'\s\\]*)?', re.I)
STREAM_URL_RE = re.compile(r'https?://[^<>"\'\s\\]+?(?:\.m3u8?|/hls/|/live/)[^<>"\'\s\\]*', re.I)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._fallback_label: str = ""
        self._text: list[str] = []

    def _flush_anchor(self) -> None:
        if self._href is not None:
            label = " ".join("".join(self._text).split()) or self._fallback_label
            self.links.append((self._href, label))
            self._href = None
            self._fallback_label = ""
            self._text = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "").strip() for k, v in attrs}
        tag_lower = tag.lower()
        if tag_lower == "a" and a.get("href"):
            self._flush_anchor()
            self._href = a["href"]
            self._fallback_label = a.get("title") or a.get("aria-label") or ""
            self._text = []
            for key in ("data-src", "data-url", "data-stream", "data-hls", "playlist"):
                if a.get(key):
                    self.links.append((a[key], ""))
            return

        if tag_lower == "img" and self._href is not None and not self._fallback_label:
            self._fallback_label = a.get("alt") or a.get("title") or ""

        for key in ("href", "src", "data-src", "data-url", "data-stream", "data-hls", "playlist"):
            if a.get(key):
                self.links.append((a[key], ""))

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            self._flush_anchor()

    def close(self) -> None:
        self._flush_anchor()
        super().close()


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


def _clean_logo_url(logo: str, base_url: str = "") -> str:
    raw = unescape((logo or "").strip())
    if not raw or raw.casefold() in INVALID_LOGO_VALUES:
        return ""
    if base_url and not urlparse(raw).scheme:
        raw = urljoin(base_url, raw)
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return raw


def clean_channel_name(name: str) -> str:
    """Clean noisy suffixes, quality tags and SEO boilerplate from channel names."""
    text = " ".join(unescape(name or "").split())
    if not text:
        return ""
    text = NOISE_BRACKET_RE.sub("", text).strip()
    text = LEADING_NUMBER_RE.sub("", text).strip()
    prefix_match = COUNTRY_PREFIX_RE.match(text)
    if prefix_match:
        token = next((g for g in prefix_match.groups() if g), "")
        if _code(token, COUNTRY_CODES, upper=True):
            candidate = text[prefix_match.end():].strip()
            if candidate:
                text = candidate
    for _ in range(2):
        seo_match = SEO_PREFIX_RE.match(text)
        if seo_match:
            text = seo_match.group(1).strip()
        text = SEO_SUFFIX_RE.sub("", text).strip()
        text = NOISE_BRACKET_RE.sub("", text).strip()
    text = " ".join(text.split()).strip(" -|:/")
    return text or " ".join(unescape(name or "").split())


def channel_slug(name: str) -> str:
    """Build a URL- and database-safe slug from a channel name."""
    cleaned = clean_channel_name(name)
    folded = _fold_text(cleaned or name)
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


def _has_substantive_token(tokens: list[str]) -> bool:
    return any(len(t) >= 3 and not t.isdigit() for t in tokens)


def _canonical_channel_key(name: str) -> str:
    """Return a canonical identity key so HD/SD/regional/wrapper variants do not repeat."""
    cleaned = clean_channel_name(name)
    folded = _fold_text(cleaned or name)
    folded = NOISE_BRACKET_RE.sub(" ", folded)
    tokens = re.findall(r"[a-z0-9]+", folded)
    if not tokens:
        return ""

    while len(tokens) > 1:
        if len(tokens) >= 2 and tokens[-1].isdigit() and tokens[-2] in NUMBERED_TAIL_PARENTS:
            tokens.pop()
            tokens.pop()
            continue
        if len(tokens) >= 2 and tokens[-2] == "en" and tokens[-1] in {"vivo", "directo"}:
            tokens.pop()
            tokens.pop()
            continue
        if tokens[-1] in VARIANT_TAIL_TOKENS:
            tokens.pop()
            continue
        if tokens[-1] in CHANNEL_WRAPPER_TOKENS and _has_substantive_token(tokens[:-1]):
            tokens.pop()
            continue
        break

    if len(tokens) > 1 and tokens[0] in {"canal", "channel"} and _has_substantive_token(tokens[1:]):
        tokens = tokens[1:]

    return "-".join(tokens)


def _tvg_id_key(tvg_id: str) -> str:
    raw = _fold_text((tvg_id or "").strip())
    if not raw:
        return ""
    raw = re.split(r"[@]", raw, maxsplit=1)[0].strip()
    return raw


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Compile word-boundary matchers once, avoiding substring false positives."""
    words = _fold_text(term).split()
    pattern = r"[\W_]+".join(re.escape(word) for word in words)
    return re.compile(rf"(?<!\w){pattern}(?!\w)")


@lru_cache(maxsize=128)
def _combined_terms_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    parts = [
        r"[\W_]+".join(re.escape(w) for w in _fold_text(term).split())
        for term in words
        if term.strip()
    ]
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)")


def _match_label(text: str, rules: dict[str, tuple[str, ...]]) -> str:
    folded = _fold_text(text)
    if not folded:
        return "unknown"
    for label, words in rules.items():
        if _combined_terms_pattern(words).search(folded):
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
    raw = re.split(r"[@]", tvg_id.strip(), maxsplit=1)[0]
    match = re.search(r"[._-]([a-z]{2,3})$", raw, re.I)
    return _code(match.group(1), COUNTRY_CODES, upper=True) if match else ""


def _country_from_prefix(text: str) -> str:
    cleaned = LEADING_NUMBER_RE.sub("", text.strip())
    match = COUNTRY_PREFIX_RE.match(cleaned)
    if not match:
        return ""
    token = next((g for g in match.groups() if g), "")
    return _code(token, COUNTRY_CODES, upper=True)


def classify_channel(name: str, group: str = "",
                     attrs: dict[str, str] | None = None) -> tuple[str, str, str]:
    attrs = {key.casefold(): value for key, value in (attrs or {}).items()}
    tvg_name = attrs.get("tvg-name", "")
    tvg_id = attrs.get("tvg-id", "")
    language_value = attrs.get("tvg-language", "") or attrs.get("language", "")
    country_value = attrs.get("tvg-country", "") or attrs.get("country", "")

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
        country = (
            _country_from_tvg_id(tvg_id)
            or _country_from_prefix(name)
            or _country_from_prefix(group)
        )
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
            logo = _clean_logo_url(attrs.get("tvg-logo", ""), base_url)
            result.append(Channel(
                name=name, url=line, group=group,
                tvg_id=attrs.get("tvg-id", ""), tvg_name=attrs.get("tvg-name", ""),
                logo=logo, language=language, country=country,
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
    parser.close()
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
    for tag in ("h1", "title"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.I | re.S)
        if match:
            text = clean_channel_name(" ".join(unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split()))
            if text and _canonical_channel_key(text) not in GENERIC_CHANNEL_NAMES:
                return text
    return fallback


def _page_logo(html: str, base_url: str) -> str:
    """Extract a page logo without executing JavaScript."""
    match = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',
        html,
        re.I,
    )
    if not match:
        match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
            html,
            re.I,
        )
    if not match:
        match = re.search(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)', html, re.I)
    return _clean_logo_url(match.group(1), base_url) if match else ""


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

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
        "Range": "bytes=0-4095",
    }
    try:
        request = Request(url, headers=headers)
        with urlopen(request, timeout=max(1, timeout)) as response:
            status = int(response.status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            final_url = response.geturl()
            sample = response.read(4096)
    except HTTPError as exc:
        if exc.code in {400, 405, 416, 501}:
            exc.close()
            try:
                fallback_headers = {k: v for k, v in headers.items() if k != "Range"}
                with urlopen(Request(url, headers=fallback_headers), timeout=max(1, timeout)) as response:
                    status = int(response.status)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    final_url = response.geturl()
                    sample = response.read(4096)
            except HTTPError as retry_exc:
                content_type = retry_exc.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                state = "restricted" if retry_exc.code in {401, 403, 407, 451} else "http_error"
                try:
                    final_url = retry_exc.geturl()
                except AttributeError:
                    final_url = url
                retry_exc.close()
                return StreamCheck(state, retry_exc.code, content_type, final_url, f"HTTP {retry_exc.code}")
            except (URLError, TimeoutError, OSError, ValueError) as retry_exc:
                reason = getattr(retry_exc, "reason", retry_exc)
                return StreamCheck("unreachable", detail=str(reason)[:240])
        else:
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
    stripped = sample.lstrip(b"\xef\xbb\xbf \t\r\n")
    has_m3u_header = stripped.startswith(b"#EXTM3U")
    looks_like_html = stripped[:32].lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
    is_playlist = (
        urlparse(final_url).path.lower().endswith((".m3u", ".m3u8"))
        or content_type in playlist_types
        or has_m3u_header
    )
    if (is_playlist and not has_m3u_header) or looks_like_html or content_type == "text/html":
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


def _merge_channel_metadata(existing: Channel, incoming: Channel) -> None:
    """Enrich an existing channel with missing metadata from a duplicate entry."""
    existing.source = ", ".join(dict.fromkeys(
        x.strip() for x in f"{existing.source},{incoming.source}".split(",") if x.strip()
    ))
    for field in ("group", "tvg_id", "tvg_name", "logo", "source_url"):
        if not getattr(existing, field) and getattr(incoming, field):
            setattr(existing, field, getattr(incoming, field))
    if existing.category == "other" and incoming.category != "other":
        existing.category = incoming.category
    if existing.language == "unknown" and incoming.language != "unknown":
        existing.language = incoming.language
    if existing.country == "unknown" and incoming.country != "unknown":
        existing.country = incoming.country
    if incoming.attributes:
        if existing.attributes is None:
            existing.attributes = dict(incoming.attributes)
        else:
            for key_name, value in incoming.attributes.items():
                if value and not existing.attributes.get(key_name):
                    existing.attributes[key_name] = value


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
        _merge_channel_metadata(out[index[key]], channel)
    return out


def _channel_quality_score(channel: Channel,
                           stream_checks: dict[str, StreamCheck] | None = None) -> tuple[int, ...]:
    """Rank candidate channels by reachability, metadata completeness and URL quality."""
    check = stream_checks.get(channel.url) if stream_checks else None
    check_rank = 2 if (check and check.status == "http_ok") else (1 if check is None else 0)
    canonical = _canonical_channel_key(channel.name)
    non_generic = 1 if (canonical and canonical not in GENERIC_CHANNEL_NAMES) else 0
    has_category = 1 if channel.category != "other" else 0
    has_country = 1 if len(channel.country) == 2 else 0
    has_language = 1 if channel.language != "unknown" else 0
    preferred_lang = 1 if channel.language in {"es", "en", "pt"} else 0
    has_logo = 1 if bool(channel.logo) else 0
    has_tvg_id = 1 if bool(channel.tvg_id) else 0
    is_https = 1 if channel.url.lower().startswith("https://") else 0
    is_hls = 1 if ".m3u8" in channel.url.lower() else 0
    return (
        check_rank,
        non_generic,
        has_category + has_country + has_language + has_logo + has_tvg_id,
        preferred_lang,
        is_https + is_hls,
        has_logo,
        has_country,
        has_language,
    )


def dedupe_unique_channels(
    channels: Iterable[Channel],
    *,
    limit: int | None = None,
    stream_checks: dict[str, StreamCheck] | None = None,
    only_http_ok: bool = False,
    diversify: bool = True,
    exclude_keys: tuple[set[str], set[str], set[str]] | None = None,
) -> list[Channel]:
    """Select unique channels without repeating name/slug, tvg_id, URL or final redirect URL.

    When ``limit`` is positive, channels are ranked by quality and interleaved across
    categories so the resulting list is diverse and free of duplicates.
    """
    candidates = _dedupe(channels)
    if not candidates:
        return []

    excluded_slugs, excluded_urls, excluded_names = exclude_keys or (set(), set(), set())

    # Sort best candidates first so when duplicates exist, the highest-quality entry wins.
    ranked = sorted(
        candidates,
        key=lambda c: (
            tuple(-v for v in _channel_quality_score(c, stream_checks)),
            clean_channel_name(c.name).casefold(),
            c.url,
        ),
    )

    unique: list[Channel] = []
    seen_canonical: dict[str, Channel] = {}
    seen_slugs: set[str] = set(excluded_slugs)
    seen_names: set[str] = set(excluded_names)
    seen_tvg_ids: set[str] = set()
    seen_urls: set[str] = set(excluded_urls)

    for channel in ranked:
        if not channel.url:
            continue
        check = stream_checks.get(channel.url) if stream_checks else None
        if only_http_ok and (check is None or check.status != "http_ok"):
            continue

        cleaned_name = clean_channel_name(channel.name)
        slug = channel_slug(cleaned_name or channel.name)
        canonical = _canonical_channel_key(cleaned_name or channel.name) or slug
        tvg_key = _tvg_id_key(channel.tvg_id)
        url_key = _url_key(channel.url)
        final_url_key = _url_key(check.final_url) if (check and check.final_url) else ""

        if not slug or not canonical:
            continue
        if canonical in GENERIC_CHANNEL_NAMES and len(candidates) > 1:
            continue

        if canonical in seen_canonical:
            _merge_channel_metadata(seen_canonical[canonical], channel)
            continue
        if (
            slug in seen_slugs
            or canonical in seen_names
            or url_key in seen_urls
            or (final_url_key and final_url_key in seen_urls)
            or (tvg_key and tvg_key in seen_tvg_ids)
        ):
            continue

        normalized_channel = Channel(
            name=cleaned_name or channel.name,
            url=channel.url,
            group=channel.group,
            tvg_id=channel.tvg_id,
            tvg_name=channel.tvg_name,
            logo=channel.logo,
            language=channel.language,
            country=channel.country,
            category=channel.category,
            source=channel.source,
            source_url=channel.source_url,
            attributes=dict(channel.attributes) if channel.attributes else None,
        )
        seen_canonical[canonical] = normalized_channel
        seen_slugs.add(slug)
        seen_names.add(canonical)
        seen_urls.add(url_key)
        if final_url_key:
            seen_urls.add(final_url_key)
        if tvg_key:
            seen_tvg_ids.add(tvg_key)
        unique.append(normalized_channel)

    if not diversify:
        unique.sort(key=lambda x: (x.category, x.language, x.name.casefold(), x.url))
        return unique[:limit] if (limit and limit > 0) else unique

    by_category: dict[str, list[Channel]] = defaultdict(list)
    for item in unique:
        by_category[item.category].append(item)

    ordered_categories = [cat for cat in CATEGORY_PRIORITY if cat in by_category]
    for cat in sorted(by_category):
        if cat not in ordered_categories:
            ordered_categories.append(cat)

    interleaved: list[Channel] = []
    round_index = 0
    while True:
        added_in_round = False
        for cat in ordered_categories:
            bucket = by_category[cat]
            if round_index < len(bucket):
                interleaved.append(bucket[round_index])
                added_in_round = True
                if limit and limit > 0 and len(interleaved) >= limit:
                    return interleaved
        if not added_in_round:
            break
        round_index += 1

    return interleaved[:limit] if (limit and limit > 0) else interleaved


def select_channels_with_checks(
    channels: Iterable[Channel],
    *,
    limit: int = DEFAULT_MAX_CHANNELS,
    timeout: int = 10,
    workers: int = 4,
    only_http_ok: bool = False,
    max_probes: int = DEFAULT_MAX_STREAM_PROBES,
    exclude_keys: tuple[set[str], set[str], set[str]] | None = None,
) -> tuple[list[Channel], dict[str, StreamCheck]]:
    """Probe candidate streams in bounded batches until ``limit`` unique channels are found.

    This avoids checking 10,000+ URLs when only 20 working, non-repeating channels are needed.
    """
    candidates = _dedupe(channels)
    if not candidates:
        return [], {}

    if limit <= 0:
        checks = check_streams(candidates, timeout=timeout, workers=workers)
        selected = dedupe_unique_channels(
            candidates,
            limit=None,
            stream_checks=checks,
            only_http_ok=only_http_ok,
            exclude_keys=exclude_keys,
        )
        return selected, checks

    excluded_slugs, excluded_urls, excluded_names = exclude_keys or (set(), set(), set())

    # Group by canonical channel identity and keep up to 2 fallback URLs per channel
    # ordered by quality and interleaved by category so we probe a diverse set first.
    ranked = sorted(
        candidates,
        key=lambda c: (
            tuple(-v for v in _channel_quality_score(c)),
            clean_channel_name(c.name).casefold(),
            c.url,
        ),
    )
    by_category_primary: dict[str, list[Channel]] = defaultdict(list)
    fallbacks_by_canonical: dict[str, list[Channel]] = defaultdict(list)

    for channel in ranked:
        slug = channel_slug(channel.name)
        canonical = _canonical_channel_key(channel.name) or slug
        url_key = _url_key(channel.url)
        if not slug or not canonical or canonical in GENERIC_CHANNEL_NAMES:
            continue
        if slug in excluded_slugs or canonical in excluded_names or url_key in excluded_urls:
            continue
        if canonical not in fallbacks_by_canonical:
            by_category_primary[channel.category].append(channel)
            fallbacks_by_canonical[canonical] = []
        elif len(fallbacks_by_canonical[canonical]) < 2:
            fallbacks_by_canonical[canonical].append(channel)

    ordered_categories = [cat for cat in CATEGORY_PRIORITY if cat in by_category_primary]
    for cat in sorted(by_category_primary):
        if cat not in ordered_categories:
            ordered_categories.append(cat)

    probe_queue: list[Channel] = []
    round_index = 0
    while True:
        added = False
        for cat in ordered_categories:
            bucket = by_category_primary[cat]
            if round_index < len(bucket):
                primary = bucket[round_index]
                probe_queue.append(primary)
                canonical = _canonical_channel_key(primary.name) or channel_slug(primary.name)
                probe_queue.extend(fallbacks_by_canonical.get(canonical, ()))
                added = True
        if not added:
            break
        round_index += 1

    if not probe_queue:
        return [], {}

    probe_cap = max(limit, max_probes) if max_probes > 0 else len(probe_queue)
    probe_queue = probe_queue[:probe_cap]

    all_checks: dict[str, StreamCheck] = {}
    probed_channels: list[Channel] = []
    batch_size = max(limit, min(max(limit * 2, 24), 48))

    for offset in range(0, len(probe_queue), batch_size):
        batch = probe_queue[offset:offset + batch_size]
        batch_checks = check_streams(batch, timeout=timeout, workers=workers)
        all_checks.update(batch_checks)
        probed_channels.extend(batch)

        ok_unique = dedupe_unique_channels(
            probed_channels,
            limit=limit,
            stream_checks=all_checks,
            only_http_ok=True,
            exclude_keys=exclude_keys,
        )
        if len(ok_unique) >= limit:
            selected_urls = {c.url for c in ok_unique}
            filtered_checks = {u: chk for u, chk in all_checks.items() if u in selected_urls or u in batch_checks}
            return ok_unique[:limit], filtered_checks

    selected = dedupe_unique_channels(
        probed_channels,
        limit=limit,
        stream_checks=all_checks,
        only_http_ok=only_http_ok,
        exclude_keys=exclude_keys,
    )
    return selected, all_checks


def _fetch_site_page(source: SourceConfig, page: str, label: str,
                     timeout: int) -> tuple[list[Channel], str | None]:
    try:
        html = _fetch_text(page, timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return [], f"{source.key}: page {page}: {exc}"
    raw_name = clean_channel_name(label) if label else ""
    if not raw_name or _canonical_channel_key(raw_name) in GENERIC_CHANNEL_NAMES:
        raw_name = _page_title(html, source.name)
    name = clean_channel_name(raw_name) or raw_name or source.name
    logo = _page_logo(html, page)
    found: list[Channel] = []
    for stream in _urls_from_html(html, page):
        if ".m3u8" not in stream.lower() and ".m3u" not in stream.lower():
            continue
        category, language, country = classify_channel(name, source.name)
        found.append(Channel(
            name=name, url=stream, group=source.name, logo=logo,
            language=language, country=country, category=category,
            source=source.name, source_url=page,
        ))
    return found, None


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
    parser.close()
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
    seen_pages: dict[str, int] = {source.url: -1}
    for href, label in parser.links:
        page = _clean_url(href, source.url)
        parsed = urlparse(page)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host:
            continue
        if source.page_prefixes and not any(
            parsed.path.lower().startswith(prefix.lower()) for prefix in source.page_prefixes
        ):
            continue
        if page in seen_pages:
            idx = seen_pages[page]
            if idx >= 0 and not pages[idx][1] and label:
                pages[idx] = (page, label)
            continue
        if len(pages) >= max_pages:
            break
        seen_pages[page] = len(pages)
        pages.append((page, label))

    if pages:
        page_workers = max(1, min(6, len(pages)))
        page_results: list[tuple[list[Channel], str | None]] = [([], None) for _ in pages]
        with ThreadPoolExecutor(max_workers=page_workers) as pool:
            futures = {
                pool.submit(_fetch_site_page, source, page, label, timeout): idx
                for idx, (page, label) in enumerate(pages)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    page_results[idx] = future.result()
                except Exception as exc:
                    page_results[idx] = ([], f"{source.key}: page {pages[idx][0]}: {exc}")

        for page_channels, page_error in page_results:
            channels.extend(page_channels)
            if page_error:
                errors.append(page_error)

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

    if fmt == "m3u":
        lines = ["#EXTM3U"]
        for channel in channel_list:
            attrs = []
            if channel.tvg_id:
                attrs.append(f'tvg-id="{channel.tvg_id}"')
            if channel.tvg_name:
                attrs.append(f'tvg-name="{channel.tvg_name}"')
            if channel.logo:
                attrs.append(f'tvg-logo="{channel.logo}"')
            if channel.language and channel.language != "unknown":
                attrs.append(f'tvg-language="{channel.language}"')
            if channel.country and channel.country != "unknown":
                attrs.append(f'tvg-country="{channel.country}"')
            group_title = channel.group or channel.category
            if group_title:
                attrs.append(f'group-title="{group_title}"')
            attr_str = (" " + " ".join(attrs)) if attrs else ""
            lines.append(f"#EXTINF:-1{attr_str},{channel.name}")
            lines.append(channel.url)
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
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


def fetch_existing_supabase_channels(url: str, key: str,
                                     timeout: int = 15) -> tuple[set[str], set[str], set[str]]:
    """Fetch existing slugs, stream URLs and canonical names from public.tv_channels."""
    endpoint = url.rstrip("/") + "/rest/v1/tv_channels?select=slug,stream_url,name&limit=5000"
    request = Request(
        endpoint,
        method="GET",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(10 * 1024 * 1024)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return set(), set(), set()

    if not isinstance(payload, list):
        return set(), set(), set()

    slugs: set[str] = set()
    urls: set[str] = set()
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip().casefold()
        stream_url = str(item.get("stream_url") or "").strip()
        name = str(item.get("name") or "").strip()
        if slug:
            slugs.add(slug)
            canonical_from_slug = _canonical_channel_key(slug.replace("-", " "))
            if canonical_from_slug:
                names.add(canonical_from_slug)
        if stream_url:
            urls.add(_url_key(stream_url))
        if name:
            canonical = _canonical_channel_key(name)
            if canonical:
                names.add(canonical)
            name_slug = channel_slug(name)
            if name_slug:
                slugs.add(name_slug)
    return slugs, urls, names


def sync_to_supabase(
    channels: Iterable[Channel],
    url: str,
    key: str,
    stream_checks: dict[str, StreamCheck] | None = None,
    activation_mode: str = "manual",
    default_country: str = "US",
    max_channels: int = DEFAULT_MAX_CHANNELS,
    existing_keys: tuple[set[str], set[str], set[str]] | None = None,
) -> int:
    """Upsert up to ``max_channels`` unique channels into public.tv_channels using Supabase REST.

    The service-role key is read from the environment and is never written to output.
    In automatic mode only streams returning HTTP 2xx are activated and synced.
    Duplicate slugs, canonical names and stream URLs are strictly filtered out.
    """
    # Deliberadamente fijo: esta integración solo escribe en public.tv_channels,
    # nunca en tv_channel, channels u otra tabla del proyecto.
    endpoint = url.rstrip("/") + "/rest/v1/tv_channels?on_conflict=slug"
    only_ok = activation_mode == "automatic" and stream_checks is not None

    unique_channels = dedupe_unique_channels(
        channels,
        limit=max_channels if max_channels > 0 else None,
        stream_checks=stream_checks,
        only_http_ok=only_ok,
        exclude_keys=existing_keys,
    )

    rows: list[dict[str, object]] = []
    seen_slugs: set[str] = set()
    seen_urls: set[str] = set()
    seen_names: set[str] = set()

    for channel in unique_channels:
        cleaned_name = clean_channel_name(channel.name) or channel.name.strip()
        slug = channel_slug(cleaned_name)
        canonical = _canonical_channel_key(cleaned_name) or slug
        url_key = _url_key(channel.url)
        if not slug or not channel.url:
            continue
        if slug in seen_slugs or canonical in seen_names or url_key in seen_urls:
            continue

        check = stream_checks.get(channel.url) if stream_checks else None
        if only_ok and (check is None or check.status != "http_ok"):
            continue
        active = True if activation_mode == "manual" else bool(check and check.status == "http_ok")
        country = (channel.country if len(channel.country) == 2 else default_country).upper()

        seen_slugs.add(slug)
        seen_names.add(canonical)
        seen_urls.add(url_key)
        rows.append({
            "name": cleaned_name,
            "slug": slug,
            "logo_url": _clean_logo_url(channel.logo) or None,
            "stream_url": channel.url,
            "stream_type": "hls",
            "category": channel.category,
            "country_code": country,
            "is_active": active,
        })
        if max_channels > 0 and len(rows) >= max_channels:
            break

    if not rows:
        return 0

    payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    last_error: Exception | None = None
    for attempt in range(2):
        request = Request(endpoint, data=payload, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                if response.status >= 300:
                    raise RuntimeError(f"Supabase HTTP {response.status}")
                return len(rows)
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(512).decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            finally:
                exc.close()
            last_error = RuntimeError(
                f"No se pudo sincronizar con Supabase: HTTP {exc.code}"
                + (f" ({detail[:200]})" if detail else "")
            )
            if exc.code in {429, 500, 502, 503, 504} and attempt == 0:
                time.sleep(1)
                continue
            raise last_error from exc
        except (URLError, OSError) as exc:
            last_error = RuntimeError(f"No se pudo sincronizar con Supabase: {exc}")
            if attempt == 0:
                time.sleep(1)
                continue
            raise last_error from exc

    if last_error:
        raise last_error
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
    parser.add_argument("--format", choices=("json", "csv", "m3u"), default=None)
    parser.add_argument("--category")
    parser.add_argument("--language")
    parser.add_argument("--country")
    parser.add_argument(
        "--limit", "--max-channels", dest="limit", type=int, default=DEFAULT_MAX_CHANNELS,
        help=f"Maximum number of unique non-repeating channels to export/sync (default: {DEFAULT_MAX_CHANNELS}; 0 for unlimited)",
    )
    parser.add_argument(
        "--max-checks", type=int, default=DEFAULT_MAX_STREAM_PROBES,
        help=f"Maximum candidate URLs to probe when --check-streams and --limit are used (default: {DEFAULT_MAX_STREAM_PROBES})",
    )
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=25)
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

    supabase_url = ""
    supabase_key = ""
    existing_keys: tuple[set[str], set[str], set[str]] = (set(), set(), set())
    if args.sync_supabase:
        # Se aceptan los nombres habituales y las variantes usadas por el proyecto.
        supabase_url = (
            os.environ.get("SUPABASE_URL")
            or os.environ.get("Supabase_URL")
            or os.environ.get("supabase_url")
            or ""
        )
        supabase_key = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_SECRET_KEY")
            or os.environ.get("SUPABASE_KEY")
            or os.environ.get("Secret_Key")
            or os.environ.get("SUPABASE_SECRET")
            or ""
        )
        if not supabase_url or not supabase_key:
            parser.error("--sync-supabase requiere SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SECRET_KEY)")
        if args.activation_mode == "automatic" and not args.check_streams:
            parser.error("--activation-mode automatic requiere también --check-streams")
        existing_keys = fetch_existing_supabase_channels(
            supabase_url, supabase_key, timeout=max(1, args.timeout),
        )

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

    limit = max(0, args.limit)
    only_http_ok = args.activation_mode == "automatic" and args.check_streams
    candidate_pool = list(channels)

    stream_checks: dict[str, StreamCheck] | None = None
    if args.check_streams:
        channels, stream_checks = select_channels_with_checks(
            candidate_pool,
            limit=limit,
            timeout=max(1, args.timeout),
            workers=max(1, args.workers),
            only_http_ok=only_http_ok,
            max_probes=max(0, args.max_checks),
            exclude_keys=existing_keys if any(existing_keys) else None,
        )
        summary = Counter(check.status for check in stream_checks.values())
        counts = ", ".join(f"{status}={count}" for status, count in sorted(summary.items()))
        print(f"Checked {len(stream_checks)} stream URLs: {counts or 'none'}")
    else:
        channels = dedupe_unique_channels(
            candidate_pool,
            limit=limit if limit > 0 else None,
            exclude_keys=existing_keys if any(existing_keys) else None,
        )

    suffix = args.output.suffix.lower()
    default_fmt = "csv" if suffix == ".csv" else ("m3u" if suffix in {".m3u", ".m3u8"} else "json")
    fmt = args.format or default_fmt
    write_output(channels, args.output, fmt, stream_checks=stream_checks)
    print(f"Exported {len(channels)} channels to {args.output}")

    if args.sync_supabase:
        try:
            synced = sync_to_supabase(
                channels,
                supabase_url,
                supabase_key,
                stream_checks,
                args.activation_mode,
                args.supabase_country,
                max_channels=limit if limit > 0 else DEFAULT_MAX_CHANNELS,
                existing_keys=existing_keys if any(existing_keys) else None,
            )
        except RuntimeError as exc:
            parser.error(str(exc))
        print(f"Sincronizados {synced} canales en Supabase ({args.activation_mode})")

    for source_key, source_errors in sorted(errors.items()):
        for error in source_errors[:10]:
            print(f"WARNING: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
