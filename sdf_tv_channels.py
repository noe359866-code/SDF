#!/usr/bin/env python3
"""SDF TV channel extractor, classifier and remote-source loader."""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import json
import os
import random
import re
import socket
import sys
import threading
import time
import unicodedata
import codecs
import zlib
from collections import Counter, defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from concurrent.futures import as_completed
from dataclasses import asdict, dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

DEFAULT_MAX_CHANNELS = 20
DEFAULT_MAX_STREAM_PROBES = 120
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
# Las listas pueden enlazar otras listas (`.m3u` dentro de `.m3u`). Se expanden como
# máximo N sub-listas por fuente para no convertir la ejecución en un rastreo abierto.
DEFAULT_MAX_NESTED_PLAYLISTS = 12
# Canales máximos que aporta cada sub-lista desplegada (una lista por país puede
# tener miles de entradas y no debe desplazar al resto de fuentes).
MAX_NESTED_ENTRIES = 1500
# Desplegar sub-listas cuesta peticiones extra: no se hace si quedan menos segundos.
NESTED_MIN_SECONDS = 25.0
# Tamaño de la muestra leída al inspeccionar un stream: suficiente para la cabecera
# `#EXTM3U`, las variantes de un master playlist y los primeros `#EXTINF`.
STREAM_SAMPLE_BYTES = 4096
MAX_VARIANT_PROBES = 3

# Directivas que acompañan a la entrada y que un reproductor necesita para pedir el
# stream (user-agent, referer, cookies y licencias DRM).
EXTVLCOPT_RE = re.compile(r"^#EXTVLCOPT\s*:\s*([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$")
EXTHTTP_RE = re.compile(r"^#EXTHTTP\s*:\s*(.+?)\s*$")
KODIPROP_RE = re.compile(r"^#KODIPROP\s*:\s*([A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$")
# `#EXTVLCOPT:http-user-agent=...` / `#EXTVLCOPT:http-referrer=...` y sus alias.
PLAYLIST_HEADER_KEYS = {
    "http-user-agent": "User-Agent",
    "http-referrer": "Referer",
    "http-referer": "Referer",
    "user-agent": "User-Agent",
    "cookie": "Cookie",
    "http-cookie": "Cookie",
}
# Variantas de un master playlist HLS (resolución, bitrate y URI de la media playlist).
HLS_STREAM_INF_RE = re.compile(r"^#EXT-X-STREAM-INF\s*:\s*(?P<attrs>.*?)\s*$", re.I)
# En un tag HLS los valores no llevan espacios y acaban en coma: ATTR_RE sería codicioso.
HLS_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^",\s]*)')
HLS_MEDIA_RE = re.compile(r"^#EXT-X-MEDIA\s*:\s*(?P<attrs>.*?)\s*$", re.I)
HLS_TARGET_DURATION_RE = re.compile(r"^#EXT-X-TARGETDURATION\s*:\s*([\d.]+)", re.I | re.M)
HLS_PLAYLIST_TYPE_RE = re.compile(r"^#EXT-X-PLAYLIST-TYPE\s*:\s*(VOD|EVENT)", re.I | re.M)
RESOLUTION_RE = re.compile(r"^\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*$")
BASE64_BLOB_RE = re.compile(r"""atob\(\s*["']([A-Za-z0-9+/=\s]{16,4096})["']\s*\)""")
JS_STRING_UNESCAPE_RE = re.compile(r"\\(?:[\"'\\/]|u00(?:22|27|5c|2f)|x(?:22|27|5c))", re.I)
# Claves habituales de los reproductores JS (`file:`, `sources:[{file:}]`, `hls:`…).
JS_STREAM_KEY_RE = re.compile(
    r"""["']?(?:file|url|src|hls|hlsurl|stream|streamurl|playlist|source|video|videourl|m3u8)["']?"""
    r"""\s*[:=]\s*["']([^"']{6,600})["']""",
    re.I,
)
EMBED_ATTR_RE = re.compile(
    r"""data-(?:iframe|embed|player|src-iframe|url-iframe)\s*=\s*["']([^"']{6,600})["']""",
    re.I,
)
DATA_ATTR_RE = re.compile(
    r"""data-[\w-]*(?:url|src|stream|file|embed|player|video|iframe|playlist)[\w-]*"""
    r"""\s*=\s*["']([^"']{6,600})["']""",
    re.I,
)
MEDIA_TAG_SRC_RE = re.compile(r"""<(?:source|video)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
META_STREAM_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["']"""
    r"""(?:og:video(?::(?:secure_)?url)?|twitter:player(?::stream)?)["'][^>]*?content\s*=\s*["']([^"']+)"""
    r"""|<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]*?(?:property|name)\s*=\s*["']"""
    r"""(?:og:video(?::(?:secure_)?url)?|twitter:player(?::stream)?)["']""",
    re.I,
)
DOCUMENT_WRITE_RE = re.compile(r"""document\s*\.\s*write(?:ln)?\s*\(\s*(.{0,2000}?)\s*\)""", re.I | re.S)

ATTR_RE = re.compile(r'([\w-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s]*)')

CATEGORY_RULES = {
    "sports": ("sport", "sports", "futbol", "football", "soccer", "deporte", "deportes",
               "esporte", "esportes", "desporto", "desportos", "calcio", "fussball",
               "espn", "fox sports", "bein", "sky sport", "dazn", "golf", "tennis", "nba",
               "basketball", "baseball", "hockey", "rugby", "cricket", "formula 1", "formula one",
               "f1", "ufc", "boxing", "volleyball", "wrestling", "motorsport", "tyc sports",
               "tudn", "win sports", "gol tv", "goltv", "teledeporte", "directv sports",
               "ovacion", "afizzionados", "claro sports", "спорт", "футбол", "رياضة", "رياضي",
               "体育", "體育", "運動", "スポーツ", "스포츠", "체육", "खेल", "olahraga", "กีฬา",
               "the thao"),
    "news": ("news", "noticias", "actualidad", "notizie", "nachrichten", "novosti", "новости",
             "أخبار", "新闻", "新聞", "ニュース", "뉴스", "समाचार", "haber", "informasi",
             "cnn", "bbc news", "al jazeera", "euronews",
             "24 horas", "franceinfo", "sky news", "fox news", "abc news", "nbc news",
             "msnbc", "reuters", "dw", "rt", "telesur", "ntn24", "adn 40", "milenio",
             "todo noticias", "c5n", "canal n", "24h", "informativo", "informativos",
             "france 24", "cbc news", "telediario", "noticiero",
             "tn", "cronica", "canal 26", "el destape", "ip noticias", "la nacion"),
    "movies": ("movie", "movies", "cinema", "cine", "pelicula", "peliculas", "film", "filme",
               "кино", "电影", "電影", "映画", "영화", "sinema", "فیلم", "फिल्म",
               "hbo", "cinemax", "paramount movies", "film4", "tcm", "space", "golden",
               "de pelicula", "studio universal", "cinecanal", "amc", "fxm", "syfy",
               "star action", "multipremier", "cinelatino", "dark", "somos"),
    "series": ("series", "serie", "séries", "drama", "dramas", "comedy", "comedies", "sitcom",
               "fiction", "novelas", "telenovela", "telenovelas", "сериалы", "电视剧", "드라마",
               "ドラマ", "dizi", "diziler", "warner", "sony channel", "universal tv", "axn",
               "tnt series", "atreseries", "las estrellas", "tlc", "pasiones", "tlnovelas",
               "distrito comedia", "comedy central", "factoria de ficcion", "fdf", "neox", "nova"),
    "kids": ("kids", "junior", "children", "infantil", "ninos", "niños", "enfants", "jeunesse",
             "kinder", "детский", "детское", "أطفال", "儿童", "兒童", "キッズ", "어린이", "बच्चों",
             "anak anak", "cartoon", "disney", "nickelodeon", "nick jr", "baby", "animation",
             "animacion", "cartoons", "clan", "boing", "discovery kids", "cartoonito", "pakapaka",
             "tooncast", "anime", "bitme", "semillitas", "babyfirst", "dreamworks"),
    "documentary": ("documentary", "documental", "documentales", "documentario", "documentaire",
                    "dokumentar", "документальный", "وثائقي", "纪录片", "紀錄片", "ドキュメンタリー",
                    "다큐멘터리", "वृत्तचित्र", "discovery", "history", "nat geo", "national geographic",
                    "smithsonian", "science", "bbc earth", "animal planet", "odisea", "docu", "historia",
                    "natgeo", "crimen", "investigation", "dmax", "be mad"),
    "music": ("music", "musica", "musique", "musik", "музыка", "موسيقى", "音乐", "音樂", "音楽",
              "음악", "संगीत", "музика", "mtv", "vh1", "concert", "concierto", "conciertos",
              "telehit", "htv", "hit tv", "kiss tv", "mezzo", "bandamax", "ritmoson",
              "quiero musica", "vmusica", "stingray", "trace"),
    "business": ("business", "financial", "finance", "economy", "economia", "markets", "economie",
                 "wirtschaft", "negocios", "finanzas", "бизнес", "экономика", "اقتصاد", "财经", "財經",
                 "経済", "경제", "अर्थशास्त्र", "cnbc", "bloomberg", "intereconomia"),
    "culture": ("culture", "cultura", "arts", "arte", "kultur", "kultura", "文化", "ثقافة", "культура",
                "संस्कृति", "lifestyle", "educativo", "cultural", "encuentro", "canal 22", "once",
                "tv unam", "senal colombia", "ciudad magazine", "la 2", "33", "13c", "ingenio"),
    "travel": ("travel", "viajes", "turismo", "tourism", "viagem", "voyage", "reisen", "viaggi",
               "путешествия", "سفر", "旅行", "여행", "यात्रा", "du lich", "sun channel", "intriper", "hola tv"),
    "cooking": ("cooking", "cook", "cocina", "cozinha", "cuisine", "kuche", "cucina", "кулинария",
                "طبخ", "美食", "料理", "요리", "खाना", "food", "comida", "gastronomia", "el gourmet",
                "canal cocina", "food network", "gusto tv"),
    "religious": ("religious", "religion", "religião", "religioso", "church", "iglesia", "gospel",
                  "kirche", "религия", "دين", "ديني", "宗教", "종교", "धर्म", "ewtn", "enlace",
                  "catolico", "cristiano", "bethel", "cristovision", "orbe 21"),
    "weather": ("weather", "clima", "meteorologia", "meteo", "wetter", "погода", "طقس", "天气", "天氣",
                "天気", "날씨", "मौसम", "hava", "accuweather", "weather channel", "eltiempo"),
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
    # Common non-ISO aliases used in feeds and locale identifiers.
    "EL": "GR", "XK": "XK", "XKX": "XK",
})
COUNTRY_RULES["XK"] = ("kosovo", "kosova")


def _load_iso_metadata() -> dict[str, object]:
    """Load bundled ISO/CLDR names; no third-party package is needed at runtime."""
    path = Path(__file__).resolve().parent / "data" / "iso_metadata.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


_ISO_METADATA = _load_iso_metadata()
for _country in _ISO_METADATA.get("countries", []):
    if not isinstance(_country, dict):
        continue
    _alpha2 = str(_country.get("alpha2") or "").upper()
    _alpha3 = str(_country.get("alpha3") or "").upper()
    if not _alpha2:
        continue
    COUNTRY_CODES[_alpha2] = _alpha2
    if _alpha3:
        COUNTRY_CODES[_alpha3] = _alpha2
    _names = _country.get("names", [])
    if not isinstance(_names, list):
        _names = []
    _aliases = tuple(str(name).strip() for name in _names if str(name).strip())
    COUNTRY_RULES[_alpha2] = tuple(dict.fromkeys((*COUNTRY_RULES.get(_alpha2, ()), *_aliases)))

_all_language_codes = _ISO_METADATA.get("language_codes", {})
if isinstance(_all_language_codes, dict):
    for _alias, _language_code in _all_language_codes.items():
        _alias = str(_alias).casefold()
        _language_code = str(_language_code).casefold()
        if _alias and _language_code and _language_code not in {"und", "mul", "mis", "zxx"}:
            LANGUAGE_CODES[_alias] = _language_code

for _language in _ISO_METADATA.get("languages", []):
    if not isinstance(_language, dict):
        continue
    _language_code = str(_language.get("code") or "").casefold()
    if not _language_code:
        continue
    _codes = _language.get("aliases", [])
    if isinstance(_codes, list):
        for _alias in _codes:
            _alias = str(_alias).casefold()
            if _alias:
                LANGUAGE_CODES[_alias] = _language_code
    _names = _language.get("names", [])
    if not isinstance(_names, list):
        _names = []
    _aliases = tuple(str(name).strip() for name in _names if str(name).strip())
    # Names cover CLDR's localized language labels; all ISO 639-3 codes are
    # accepted separately through LANGUAGE_CODES, even when no display name exists.
    if _aliases:
        LANGUAGE_RULES[_language_code] = tuple(dict.fromkeys(
            (*LANGUAGE_RULES.get(_language_code, ()), *_aliases)
        ))

# Prefer the most specific country names when names overlap (e.g. Congo vs.
# Democratic Republic of the Congo). Dict insertion order keeps ties deterministic.
COUNTRY_RULES = dict(sorted(
    ((code, tuple(sorted(terms, key=len, reverse=True))) for code, terms in COUNTRY_RULES.items()),
    key=lambda item: max((len(term) for term in item[1]), default=0),
    reverse=True,
))

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
SEO_TAIL_TERM = (
    r"(?:en\s+(?:vivo|directo)(?:\s+online)?|online(?:\s+gratis)?|gratis|"
    r"live\s+stream(?:ing)?|ver\s+(?:canal|ahora|en\s+vivo|en\s+directo)|"
    r"watch\s+(?:live|now|channel))"
)
SEO_SUFFIX_RE = re.compile(
    r"\s*(?:[-|–—:]\s*(?:cxtv(?:\s*en\s*vivo)?|teleonline|tv\s*en\s*vivo|en\s*vivo|en\s*directo|"
    r"online\s*gratis|ver(?:\s+(?:tv|online|ahora|el\s+canal|la\s+se[ñn]al))?).*|"
    rf"\b{SEO_TAIL_TERM}(?:\s*(?:y|,|[-|–—:])\s*{SEO_TAIL_TERM})*\s*)$",
    re.I,
)
VARIANT_TAIL_TOKENS = {
    "hd", "fhd", "uhd", "4k", "8k", "sd", "1080p", "1080i", "720p", "576p",
    "480p", "360p", "hevc", "h264", "h265", "50fps", "60fps", "live",
    "online", "stream", "streaming", "backup", "mirror", "gratis",
    "hls", "m3u8", "oficial",
}
# Geographic editions are meaningful channel identities, not removable quality tags.
REGIONAL_CHANNEL_TOKENS = {
    "latam", "latinoamerica", "sur", "norte", "este", "oeste", "east", "west",
    "internacional", "international", "int",
}
CHANNEL_WRAPPER_TOKENS = {"tv", "television", "channel", "canal"}
NUMBERED_TAIL_PARENTS = {"senal", "signal", "opc", "opcion", "option", "server", "feed", "fuente"}
GENERIC_CHANNEL_NAMES = {
    "", "unnamed", "unnamed-channel", "channel", "canal", "tv", "live",
    "stream", "cxtv", "teleonline", "m3u-cl", "tdtchannels", "iptv-org",
    "bbyte", "bbyte-jellyfin", "unknown", "test", "playlist",
}
INVALID_LOGO_VALUES = {"", "n/a", "na", "null", "none", "undefined", "false", "0"}

STREAM_TYPE_HLS = "hls"
STREAM_TYPE_YOUTUBE = "youtube"

# Rutas que suelen ser un reproductor intermedio en el que hay que entrar (iframe,
# wrapper `/html/fl/`, `core.php`, `cvatt.html`, `/embed/`…).
PLAYER_URL_HINTS = (
    "iframe", "player", "reproductor", "embed", "/cvatt", "core.php", "/html/",
    "vercanal", "watch", "play.php", "stream.php", "en-vivo", "envivo",
)

# ``urlopen`` nunca devuelve el HTML "escapado" que sí aparece dentro de JS/JSON:
# estas expresiones trabajan sobre una copia ya des-escapada del documento.
ESCAPED_SLASH_RE = re.compile(r"\\/")
UNICODE_ESCAPE_RE = re.compile(r"\\u00(?:26|3d|3f|2f)", re.I)
UNICODE_ESCAPE_MAP = {"\\u0026": "&", "\\u003d": "=", "\\u003f": "?", "\\u002f": "/"}

IFRAME_SRC_RE = re.compile(r"<iframe[^>]+?src\s*=\s*[\"']([^\"']+)[\"']", re.I)
JS_STRING_RE = re.compile(r"[\"']((?:https?:)?//[^\"'\s<>\\]+|/[^\"'\s<>\\]{2,300})[\"']")
JSON_ENDPOINT_RE = re.compile(r"[\"']([^\"'\s<>\\]*\.json(?:\?[^\"'\s<>\\]*)?)[\"']", re.I)
BASIC_JSON_NAME_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", re.I,
)

YOUTUBE_EMBED_RE = re.compile(
    r"(?:https?:)?//(?:www\.|m\.)?(?:youtube(?:-nocookie)?\.com/(?:embed|live|v)/|youtu\.be/)"
    r"([A-Za-z0-9_-]{6,})",
    re.I,
)
YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}
YOUTUBE_LIVE_MARKERS = (
    '"islive":true',
    '"islivecontent":true',
    '"islivebroadcast":true',
    '"livebroadcastcontent":"live"',
    '"livestream":true',
    "livestreamability",
)

# Cuántos documentos como máximo se descargan para resolver un canal y cuántos
# candidatos se siguen por documento (evita que un reproductor con anuncios
# encadene decenas de peticiones).
MAX_PLAYER_DOCS = 6
MAX_PLAYER_CANDIDATES = 6
MAX_CONFIG_ENDPOINTS = 2


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
    stream_type: str = STREAM_TYPE_HLS
    attributes: dict[str, str] | None = None
    # Cabeceras HTTP que el reproductor necesita (User-Agent/Referer/Cookie). Se leen
    # de `#EXTVLCOPT`/`#EXTHTTP` en la lista o del propio sitio cuando es un scrapeo.
    headers: dict[str, str] | None = None
    # Directivas de entrada que no se traducen a cabeceras (`#EXT-X-KEY`, un
    # `#KODIPROP:license_type`, un `#EXTVLCOPT:http-proxy`…) y que por tanto hay que
    # reescribir literalmente al exportar, si no la lista deja de reproducirse.
    directives: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HlsVariant:
    """Una variante de un master playlist HLS."""

    url: str
    bandwidth: int = 0
    average_bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    name: str = ""


@dataclass(frozen=True)
class HlsPlaylist:
    """Resultado de inspeccionar el cuerpo de una playlist HLS."""

    kind: str = ""  # "master" | "media" | "empty" | ""
    variants: tuple[HlsVariant, ...] = ()
    media_urls: tuple[str, ...] = ()
    segment_count: int = 0
    target_duration: float = 0.0
    is_live: bool | None = None
    encrypted: bool = False
    truncated: bool = False

    @property
    def best_variant(self) -> HlsVariant | None:
        return self.variants[0] if self.variants else None


@dataclass(frozen=True)
class StreamCheck:
    """A bounded HTTP reachability check; it does not guarantee playback."""

    status: str
    http_status: int | None = None
    content_type: str = ""
    final_url: str = ""
    detail: str = ""
    # Inspección del propio `.m3u8`: tipo de playlist, variante jugable resuelta y
    # número de segmentos encontrados en la muestra leída.
    playlist_kind: str = ""
    media_url: str = ""
    resolution: str = ""
    segment_count: int = 0
    is_live: bool | None = None


LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
LOG_LEVEL = LOG_LEVELS["INFO"]


def configure_logging(level: str | int = "INFO") -> int:
    """Fija el nivel de detalle de la salida (acepta nombres o enteros)."""
    global LOG_LEVEL
    if isinstance(level, int):
        LOG_LEVEL = level
    else:
        normalized = str(level).strip().upper()
        if normalized not in LOG_LEVELS:
            raise ValueError(
                f"nivel de log no válido: {level!r} (elige {', '.join(LOG_LEVELS)})"
            )
        LOG_LEVEL = LOG_LEVELS[normalized]
    return LOG_LEVEL


def log(message: str = "", level: str = "info", file: Any = None,
        end: str = "\n") -> None:
    """Imprime solo lo que el nivel activo pide (`debug`/`info`/`warning`/`error`).

    Los avisos se envían a stderr en las llamadas que ya lo hacían; así `--log-level`
    sirve para silenciar el ruido del rastreo en CI o para verlo todo con `debug`.
    """
    if LOG_LEVELS.get(str(level).upper(), LOG_LEVELS["INFO"]) < LOG_LEVEL:
        return
    print(message, end=end, file=file if file is not None else sys.stdout, flush=True)


@dataclass(frozen=True)
class SourceConfig:
    """Descripción de una fuente remota.

    ``kind="site"`` recorre las páginas del sitio (``page_prefixes``/``page_patterns``)
    y entra en los reproductores/iframes que encuentra. ``sitemap_urls`` permite
    descubrir páginas que no aparecen en el HTML inicial; ``country_path_prefix``
    usa un código de región presente en la ruta como metadato explícito. ``embed_hosts``
    amplía los dominios permitidos y ``max_depth`` limita los saltos del reproductor.
    """

    key: str
    name: str
    url: str
    kind: str = "playlist"
    playlist_hints: tuple[str, ...] = ()
    page_prefixes: tuple[str, ...] = ()
    page_patterns: tuple[str, ...] = ()
    embed_hosts: tuple[str, ...] = ()
    max_depth: int = 2
    fallback_urls: tuple[str, ...] = ()
    sitemap_urls: tuple[str, ...] = ()
    country_hint: str = ""
    country_path_prefix: str = ""
    title_pattern: str = ""
    # Cabeceras extra para descargar ESTA fuente ("Key: Value"), útil cuando el
    # servidor exige un Referer propio o un User-Agent concreto.
    headers: tuple[str, ...] = ()
    # Listas anidadas (`.m3u` que enlazan otros `.m3u`) a expandir tras el parseo.
    expand_nested: bool = True


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
    SourceConfig("teleonline_m3u", "Teleonline M3U",
                 "https://teleonline.github.io/listas/tv.m3u8"),
    # Sitios con reproductor propio (WordPress, wrappers PHP/iframe y YouTube en vivo).
    SourceConfig("teleonline_tv", "Teleonline TV", "https://www.teleonline.tv/", "site",
                 page_prefixes=("/canal/",), max_depth=1),
    SourceConfig("tvenvivo", "TV en Vivo", "https://www.tvenvivo.org/", "site",
                 page_patterns=(r"-en-vivo(?:-online)?\.php$", r"/canal"), max_depth=2),
    SourceConfig("tvlibreonline", "TV Libre Online", "https://tvlibreonline.st/", "site",
                 page_prefixes=("/en-vivo/",), max_depth=3),
    # TV Garden publica un sitemap global; /tv/{país}/ aporta una señal de país
    # más fiable que intentar inferirlo desde menciones ambiguas del título.
    SourceConfig("tvgarden", "TV Garden", "https://tvgarden.world/", "site",
                 page_patterns=(r"^/tv/[a-z]{2}/[^/]+/?$",),
                 sitemap_urls=("https://tvgarden.world/sitemap_tv.xml",),
                 country_path_prefix="/tv/",
                 title_pattern=r"^(.*?)\s+-\s+Watch\b",
                 embed_hosts=("raw.githubusercontent.com",)),
    SourceConfig("samsungtvplus", "Samsung TV Plus",
                 "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/samsungtvplus_all.m3u"),
    SourceConfig("uslg", "LG Channels US", "https://www.apsattv.com/uslg.m3u",
                 country_hint="US"),
    # Se conserva el enlace indicado; al comprobarlo, el endpoint respondía 404.
    SourceConfig("tubi", "Tubi",
                 "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/tubi_all.m3u",
                 country_hint="US"),
    SourceConfig("plex", "Plex",
                 "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/plex_all.m3u"),
    SourceConfig("vizio", "Vizio WatchFree", "https://www.apsattv.com/vizio.m3u",
                 country_hint="US"),
    SourceConfig("xiaomi", "Xiaomi", "https://www.apsattv.com/xiaomi.m3u"),
    # Los slugs oficiales en Apsattv usan guion bajo; se mantienen los slugs
    # solicitados como alternativas para no perderlos si el servidor los habilita.
    SourceConfig("rakuten_uk", "Rakuten TV UK", "https://www.apsattv.com/rakuten_uk.m3u",
                 fallback_urls=("https://www.apsattv.com/rakutentv-uk.m3u",),
                 country_hint="GB"),
    SourceConfig("rakuten_fr", "Rakuten TV France", "https://www.apsattv.com/rakuten_fr.m3u",
                 fallback_urls=("https://www.apsattv.com/rakuten-fr.m3u",),
                 country_hint="FR"),
    SourceConfig("movieark_br", "Movie Ark Brasil", "https://www.apsattv.com/moviearkbr.m3u",
                 country_hint="BR"),
    SourceConfig("cineverse", "Cineverse", "https://www.apsattv.com/cineverse.m3u",
                 country_hint="US"),
)

USER_AGENT = "SDF-TV-Channel-Extractor/1.4"
# Muchos servidores rechazan agentes no navegador, por lo que las peticiones usan
# cabeceras realistas y todavía se identifican mediante USER_AGENT en las pruebas.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "application/x-mpegURL,text/plain;q=0.8,*/*;q=0.5"),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}
# Caché por ejecución: las páginas de un mismo sitio comparten el JSON de configuración
# y el wrapper del reproductor, así que un solo intento de red los resuelve todos.
FETCH_CACHE_TTL = 300.0
FAILURE_CACHE_TTL = 45.0
FETCH_CACHE_SIZE = 512
MAX_CACHED_BYTES = 512 * 1024
# Intervalo mínimo entre peticiones al mismo host (cortesía con los servidores).
POLITE_DELAY = float(os.environ.get("SDF_POLITE_DELAY", "0.15"))
try:  # brotli es opcional: solo se anuncia si está instalado.
    import brotli  # type: ignore[import-not-found]

    _BROTLI_AVAILABLE = True
except ImportError:  # pragma: no cover - depende del entorno
    _BROTLI_AVAILABLE = False

_CACHE_LOCK = threading.Lock()
_FETCH_CACHE: "OrderedDict[str, tuple[float, str | None, str]]" = OrderedDict()
_HOST_LOCK = threading.Lock()
_HOST_LAST: dict[str, float] = {}


class CachedFetchError(ValueError):
    """Error recordado de un intento previo, re-lanzado sin repetir la petición."""

M3U_URL_RE = re.compile(r'(?:(?:https?:)?//|/)[^<>"\'\s\\]+?\.m3u8?(?:\?[^<>"\'\s\\]*)?', re.I)
STREAM_URL_RE = re.compile(r'https?://[^<>"\'\s\\]+?(?:\.m3u8?|/hls/|/live/)[^<>"\'\s\\]*', re.I)
SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)


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


# Some country names are also common personal names, places, foods, or programme
# titles. Treat these as country labels only when a feed supplies explicit metadata.
AMBIGUOUS_COUNTRY_NAME_TOKENS = frozenset({
    "chad", "congo", "cuba", "dominica", "georgia", "guinea", "jordan",
    "mali", "niger", "turkey", "turkiye",
})

# Single-word country names used as optional prefixes (e.g. "Afganistán Canal 7").
COUNTRY_NAME_TOKENS = frozenset(
    folded
    for values in COUNTRY_RULES.values()
    for value in values
    for folded in (_fold_text(value),)
    if len(re.findall(r"[^\W_]+", folded, flags=re.UNICODE)) == 1
    and (len(folded) >= 4 or not folded.isascii())
    and folded not in AMBIGUOUS_COUNTRY_NAME_TOKENS
)


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
    text = _strip_leading_country_name(" ".join(text.split()).strip(" -|:/"))
    return text or " ".join(unescape(name or "").split())


def _strip_leading_country_name(text: str) -> str:
    """Quita etiquetas de país de una sola palabra ("Argentina Telefe" → "Telefe").

    Solo se elimina cuando queda un nombre con contenido propio, así "Cuba TV" o
    "Panamá TV" se conservan intactos.
    """
    if not text:
        return text
    match = re.match(r"^([^\s]+)\s+(.+)$", text)
    if not match:
        return text
    first, rest = match.group(1), match.group(2)
    if _fold_text(first) not in COUNTRY_NAME_TOKENS:
        return text
    remaining = [
        token for token in re.findall(r"[^\W_]+", _fold_text(rest), flags=re.UNICODE)
        if token not in CHANNEL_WRAPPER_TOKENS and token not in VARIANT_TAIL_TOKENS
    ]
    if not any(
        (len(token) >= 3 and not token.isdigit()) or _is_unspaced_script(token)
        for token in remaining
    ):
        return text
    return rest.strip()


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
        # Keep the wrapper when it is part of a short regional name (e.g. Canal Sur).
        if not (len(tokens) == 2 and tokens[1] in REGIONAL_CHANNEL_TOKENS):
            tokens = tokens[1:]

    return "-".join(tokens)


# ---------------------------------------------------------------------------
# Búsqueda de canales específicos
# ---------------------------------------------------------------------------

# Campos que pueden usarse con --search / search_channels(). ``slug`` se deriva
# del nombre limpio y permite buscar por identificadores tipo ``espn-deportes``.
VALID_SEARCH_FIELDS: frozenset[str] = frozenset({
    "name", "group", "tvg_id", "tvg_name", "category", "country", "language",
    "source", "source_url", "url", "logo", "slug",
})
DEFAULT_SEARCH_FIELDS: tuple[str, ...] = ("name", "group", "tvg_name", "tvg_id", "category")


def _channel_field_text(channel: Channel, field: str) -> str:
    """Devuelve el texto del campo ``field`` de ``channel``."""
    key = field.strip().casefold()
    if key == "name":
        return channel.name or ""
    if key == "group":
        return channel.group or ""
    if key == "tvg_id":
        return channel.tvg_id or ""
    if key == "tvg_name":
        return channel.tvg_name or ""
    if key == "category":
        return channel.category or ""
    if key == "country":
        return channel.country or ""
    if key == "language":
        return channel.language or ""
    if key == "source":
        return channel.source or ""
    if key == "source_url":
        return channel.source_url or ""
    if key == "url":
        return channel.url or ""
    if key == "logo":
        return channel.logo or ""
    if key == "slug":
        return channel_slug(channel.name) if channel.name else ""
    # Permite buscar también dentro de atributos originales del M3U.
    if channel.attributes:
        for attr_key, attr_val in channel.attributes.items():
            if attr_key.casefold() == key:
                return str(attr_val or "")
    return ""


def channel_matches_query(
    channel: Channel,
    query: str,
    *,
    fields: Iterable[str] | None = None,
    use_regex: bool = False,
) -> bool:
    """Indica si ``channel`` satisface ``query`` en los ``fields`` indicados.

    - Búsqueda insensible a mayúsculas y acentos (usa ``_fold_text``).
    - ``query`` puede contener varias alternativas separadas por comas (OR):
      ``\"espn, fox sports\"`` coincide si aparece cualquiera de ellas.
    - Dentro de cada alternativa todas las palabras deben aparecer (AND) o bien
      la frase completa como subcadena. Esto permite tanto
      ``\"espn colombia\"`` como ``\"colombia espn\"``.
    - ``use_regex=True`` interpreta ``query`` como expresión regular aplicada
      sobre el texto combinado de los campos (también plegado).
    """
    if not query or not query.strip():
        return True
    resolved_fields = tuple(
        f.strip().casefold() for f in (fields or DEFAULT_SEARCH_FIELDS)
        if f and f.strip().casefold() in VALID_SEARCH_FIELDS
    ) or DEFAULT_SEARCH_FIELDS

    haystack_raw = " ".join(_channel_field_text(channel, f) for f in resolved_fields)
    folded_haystack = _fold_text(haystack_raw)
    haystack_tokens = set(re.findall(r"[^\W_]+", folded_haystack, flags=re.UNICODE))

    if use_regex:
        # Se pliega tanto el haystack como el patrón para que la búsqueda
        # sea insensible a acentos sin que el usuario tenga que escaparlos.
        try:
            pattern = re.compile(_fold_text(query), re.I)
        except re.error:
            return False
        return bool(pattern.search(folded_haystack))

    alternatives = [alt.strip() for alt in query.split(",") if alt.strip()]
    if not alternatives:
        return False
    for alt in alternatives:
        folded_alt = _fold_text(alt)
        if not folded_alt:
            continue
        # Coincidencia exacta de frase plegada.
        if folded_alt in folded_haystack:
            return True
        tokens = re.findall(r"[^\W_]+", folded_alt, flags=re.UNICODE)
        if not tokens:
            continue
        # Todas las palabras de la alternativa deben aparecer (AND).
        # Se acepta tanto coincidencia por token exacto como por subcadena
        # dentro del haystack, para no romper búsquedas parciales tipo \"espn\".
        if all(token in folded_haystack for token in tokens):
            # Para palabras muy cortas (<=2) exigimos token exacto para evitar
            # falsos positivos: \"tv\" no debe coincidir dentro de \"tvn\".
            short_ok = all(
                (token in haystack_tokens) if len(token) <= 2 else True
                for token in tokens
            )
            if short_ok:
                return True
    return False


def search_channels(
    channels: Iterable[Channel],
    query: str,
    *,
    fields: Iterable[str] | None = None,
    use_regex: bool = False,
) -> list[Channel]:
    """Filtra ``channels`` por ``query``.

    Args:
        channels: Iterable de :class:`Channel`.
        query: Texto a buscar. Vacío o solo espacios devuelve todos.
               Acepta comas como OR y espacios como AND dentro de cada alternativa.
               Ejemplo: ``\"cnn, bbc\"`` → CNN O BBC;
               ``\"espn colombia\"`` → contiene espn Y colombia (en cualquier orden).
        fields: Campos donde buscar. Por defecto ``name, group, tvg_name, tvg_id, category``.
                Valores válidos: ``name, group, tvg_id, tvg_name, category, country,
                language, source, source_url, url, logo, slug``. Se ignoran los no válidos.
        use_regex: Si es True, ``query`` se interpreta como expresión regular
                   (insensible a mayúsculas/acentos, aplicada sobre los campos plegados).

    Returns:
        Lista filtrada manteniendo el orden original.
    """
    if not query or not query.strip():
        return list(channels)
    resolved_fields = tuple(
        f.strip().casefold() for f in (fields or DEFAULT_SEARCH_FIELDS)
        if f and f.strip().casefold() in VALID_SEARCH_FIELDS
    ) or DEFAULT_SEARCH_FIELDS
    return [
        ch for ch in channels
        if channel_matches_query(ch, query, fields=resolved_fields, use_regex=use_regex)
    ]


def filter_channels(
    channels: Iterable[Channel],
    *,
    query: str = "",
    fields: Iterable[str] | None = None,
    use_regex: bool = False,
    category: str = "",
    language: str = "",
    country: str = "",
    source: str = "",
) -> list[Channel]:
    """Combina ``search_channels`` con filtros de categoría/idioma/país/fuente.

    Todos los filtros son opcionales y se aplican en AND, igual que las opciones del CLI;
    la función existe para filtrar una lista ya cargada desde código sin lanzar `main`.
    """
    result = list(channels)
    if query and query.strip():
        result = search_channels(result, query, fields=fields, use_regex=use_regex)
    if category and category.strip():
        wanted = category.strip().casefold()
        result = [c for c in result if c.category.casefold() == wanted]
    if language and language.strip():
        wanted = language.strip().casefold()
        result = [c for c in result if c.language.casefold() == wanted]
    if country and country.strip():
        wanted = country.strip().upper()
        result = [c for c in result if c.country.upper() == wanted]
    if source and source.strip():
        wanted = _fold_text(source.strip())
        result = [c for c in result if wanted in _fold_text(c.source or "")]
    return result


def _tvg_id_key(tvg_id: str) -> str:
    raw = _fold_text((tvg_id or "").strip())
    if not raw:
        return ""
    raw = re.split(r"[@]", raw, maxsplit=1)[0].strip()
    return raw


_UNSPACED_SCRIPT_RANGES = (
    (0x0E00, 0x0EFF),  # Thai and Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x3040, 0x30FF),  # Hiragana and Katakana
    (0x3100, 0x312F),  # Bopomofo
    (0x3400, 0x9FFF),  # CJK ideographs
    (0xAC00, 0xD7AF),  # Hangul
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x20000, 0x2FA1F),  # CJK extensions
)
_RULE_MATCHER_CACHE: dict[
    int,
    tuple[
        dict[str, tuple[str, ...]],
        dict[tuple[str, ...], str],
        dict[str, object],
        dict[str, int],
        tuple[int, ...],
    ],
] = {}


def _is_unspaced_script(text: str) -> bool:
    return any(
        start <= ord(char) <= end
        for char in text
        for start, end in _UNSPACED_SCRIPT_RANGES
    )


def _rule_matcher(
    rules: dict[str, tuple[str, ...]],
) -> tuple[dict[tuple[str, ...], str], dict[str, object], dict[str, int], tuple[int, ...]]:
    key = id(rules)
    cached = _RULE_MATCHER_CACHE.get(key)
    if cached and cached[0] is rules:
        return cached[1], cached[2], cached[3], cached[4]

    token_to_label: dict[tuple[str, ...], str] = {}
    script_trie: dict[str, object] = {}
    priority = {label: index for index, label in enumerate(rules)}
    for label, terms in rules.items():
        for term in terms:
            folded = _fold_text(term)
            tokens = tuple(re.findall(r"[^\W_]+", folded, flags=re.UNICODE))
            if tokens:
                token_to_label.setdefault(tokens, label)
            if not _is_unspaced_script(folded):
                continue
            compact_term = "".join(folded.split())
            if not compact_term:
                continue
            node = script_trie
            for char in compact_term:
                child = node.get(char)
                if not isinstance(child, dict):
                    child = {}
                    node[char] = child
                node = child
            node.setdefault("", label)

    lengths = tuple(sorted({len(term) for term in token_to_label}, reverse=True))
    _RULE_MATCHER_CACHE[key] = (rules, token_to_label, script_trie, priority, lengths)
    return token_to_label, script_trie, priority, lengths


def _match_label(text: str, rules: dict[str, tuple[str, ...]]) -> str:
    folded = _fold_text(text)
    if not folded:
        return "unknown"
    token_to_label, script_trie, priority, lengths = _rule_matcher(rules)
    best_label = "unknown"
    best_priority = len(priority)
    tokens = tuple(re.findall(r"[^\W_]+", folded, flags=re.UNICODE))
    for size in lengths:
        for start in range(len(tokens) - size + 1):
            label = token_to_label.get(tokens[start:start + size])
            if label is None:
                continue
            label_priority = priority[label]
            if label_priority < best_priority:
                best_label = label
                best_priority = label_priority
                if best_priority == 0:
                    return best_label

    compact_text = "".join(folded.split())
    for start in range(len(compact_text)):
        node = script_trie
        for char in compact_text[start:]:
            child = node.get(char)
            if not isinstance(child, dict):
                break
            node = child
            label = node.get("")
            if isinstance(label, str) and priority[label] < best_priority:
                best_label = label
                best_priority = priority[label]
                if best_priority == 0:
                    return best_label
    return best_label


def _code(value: str, mapping: dict[str, str], upper: bool = False) -> str:
    for token in re.split(r"[,;|/\s]+", value.strip()):
        candidates: list[str] = []
        if upper and ("-" in token or "_" in token):
            subtags = re.split(r"[-_]", token)
            # A country field can contain a locale (e.g. es-MX). Prefer its region
            # over the language subtag, which can itself look like a country code.
            if subtags and subtags[0].casefold() in LANGUAGE_CODES:
                candidates.extend(reversed(subtags[1:]))
            candidates.append(subtags[0])
        else:
            candidates.append(token)
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


def _country_from_channel_id(channel_id: str) -> str:
    """Read a country suffix used by provider IDs such as ``GB...-gb``."""
    raw = re.split(r"[@]", channel_id.strip(), maxsplit=1)[0]
    if re.fullmatch(r"[a-z]{2,3}", raw, re.I):
        return _code(raw, COUNTRY_CODES, upper=True)
    match = re.search(r"[._-]([a-z]{2,3})$", raw, re.I)
    return _code(match.group(1), COUNTRY_CODES, upper=True) if match else ""


def _country_from_prefix(text: str) -> str:
    cleaned = LEADING_NUMBER_RE.sub("", text.strip())
    match = COUNTRY_PREFIX_RE.match(cleaned)
    if not match:
        return ""
    token = next((g for g in match.groups() if g), "")
    return _code(token, COUNTRY_CODES, upper=True)


COUNTRY_CONNECTOR_TOKENS = frozenset({
    "de", "del", "da", "do", "dos", "das", "from", "in", "en", "of",
})
COUNTRY_PREFIX_FOLLOWERS = frozenset({
    "canal", "channel", "news", "network", "radio", "sport", "sports", "television",
    "tv", "today",
})
COUNTRY_TRAILING_CONTEXT = frozenset({
    "canal", "channel", "de", "del", "da", "do", "en", "fhd", "free", "hd",
    "live", "news", "online", "radio", "sd", "sport", "sports", "television", "tv",
    "uhd", "vivo", "8k", "4k",
})


def _country_from_marked_text(text: str, *, allow_exact: bool = False) -> str:
    """Infer a country only from a label-shaped name/group, not any mention.

    The old all-text scan classified a channel as Jordanian just because its title
    began with "Jordan Peterson", or as Georgian because a programme mentioned
    Georgia. Here a country must be the whole label, a marked prefix/suffix,
    a recognized country/channel form ("France 24", "Mexico TV"), or follow an
    explicit connector ("News from Colombia", "Canal de Sudáfrica").
    """
    folded = _fold_text(text)
    word_spans = list(re.finditer(r"[^\W_]+", folded, flags=re.UNICODE))
    if not word_spans:
        return ""
    tokens = tuple(match.group(0) for match in word_spans)
    token_to_label, _trie, _priority, lengths = _rule_matcher(COUNTRY_RULES)

    # For overlapping aliases (e.g. Democratic Republic of the Congo), keep only
    # the longest match beginning at each token, then discard nested shorter hits.
    matches: list[tuple[int, int, str]] = []
    for start in range(len(tokens)):
        for size in lengths:
            end = start + size
            if end > len(tokens):
                continue
            label = token_to_label.get(tokens[start:end])
            if label:
                matches.append((start, end, label))
                break
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    outer_matches: list[tuple[int, int, str]] = []
    for match in matches:
        if any(start <= match[0] and end >= match[1]
               for start, end, _label in outer_matches):
            continue
        outer_matches.append(match)

    candidates: set[str] = set()
    for start, end, label in outer_matches:
        prefix = folded[:word_spans[start].start()]
        suffix = folded[word_spans[end - 1].end():]
        before = tokens[:start]
        after = tokens[end:]
        alias_is_ambiguous = (
            end - start == 1 and tokens[start] in AMBIGUOUS_COUNTRY_NAME_TOKENS
        )

        exact = not before and not after
        if exact and (allow_exact or not alias_is_ambiguous):
            candidates.add(label)
            continue

        left = prefix.rstrip()
        right = suffix.lstrip()
        bracketed = (
            (left.endswith("[") and right.startswith("]"))
            or (left.endswith("(") and right.startswith(")"))
        )
        separated = bool(
            re.search(r"[-|/:,]\s*$", prefix)
            or re.match(r"\s*[-|/:,]", suffix)
        )
        connector_before = bool(before and before[-1] in COUNTRY_CONNECTOR_TOKENS)
        trailing_context = bool(after) and all(
            token in COUNTRY_TRAILING_CONTEXT or token.isdigit() for token in after
        )
        prefix_context = bool(after and after[0] in COUNTRY_PREFIX_FOLLOWERS)

        if bracketed or separated:
            candidates.add(label)
        elif not after and (not alias_is_ambiguous or connector_before):
            # Country suffixes are common in names like "ESPN Colombia"; names
            # that collide with personal names need an explicit connector.
            candidates.add(label)
        elif connector_before and (not after or trailing_context):
            candidates.add(label)
        elif not before and prefix_context and not alias_is_ambiguous:
            candidates.add(label)
        elif trailing_context and not alias_is_ambiguous:
            candidates.add(label)

    return next(iter(candidates)) if len(candidates) == 1 else ""


def classify_channel(name: str, group: str = "",
                     attrs: dict[str, str] | None = None) -> tuple[str, str, str]:
    attrs = {key.casefold(): value for key, value in (attrs or {}).items()}
    tvg_name = attrs.get("tvg-name", "")
    tvg_id = attrs.get("tvg-id", "")
    channel_id = (attrs.get("channel-id") or attrs.get("tvg-channel-id")
                  or attrs.get("channel_id", ""))
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
        country = ""
    if not country:
        country = (
            _country_from_tvg_id(tvg_id)
            or _country_from_channel_id(channel_id)
            or _country_from_prefix(name)
            or _country_from_prefix(group)
        )
    if not country:
        group_code = group.strip()
        if re.fullmatch(r"[A-Z]{2,3}", group_code) and group_code in COUNTRY_CODES:
            country = COUNTRY_CODES[group_code]
    if not country:
        country = (
            _country_from_marked_text(group, allow_exact=True)
            or _country_from_marked_text(tvg_name)
            or _country_from_marked_text(name)
        )

    return category, language, country or "unknown"


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


def _hls_attributes(text: str) -> dict[str, str]:
    """`BANDWIDTH=…,RESOLUTION="…",CODECS="a,b"` → dict de claves en minúsculas."""
    result: dict[str, str] = {}
    for match in HLS_ATTR_RE.finditer(text or ""):
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        result[match.group(1).lower()] = value.strip()
    return result


def _resolution_area(resolution: str) -> int:
    """Área en píxeles de una etiqueta ``RESOLUTION=1920x1080`` (0 si no es válida)."""
    match = RESOLUTION_RE.match(resolution or "")
    if not match:
        return 0
    return int(match.group(1)) * int(match.group(2))


def parse_hls_variants(text: str, base_url: str = "") -> tuple[HlsVariant, ...]:
    """Lee las variantes de un *master playlist* HLS, ordenadas de mejor a peor.

    Un ``.m3u8`` de un canal suele ser un master: una línea
    ``#EXT-X-STREAM-INF:BANDWIDTH=…,RESOLUTION=…`` seguida de la URL de la
    *media playlist* realmente reproducible.
    """
    if not text or "#EXT-X-STREAM-INF" not in text.upper():
        return ()
    variants: list[HlsVariant] = []
    seen: set[str] = set()
    pending: dict[str, str] | None = None
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = HLS_STREAM_INF_RE.match(line)
        if match:
            pending = _hls_attributes(match.group("attrs"))
            continue
        if pending is None:
            continue
        if line.startswith("#"):
            continue
        url = _clean_url(line, base_url) if base_url else line.strip("\"'")
        # Sin `base_url` la URI relativa sigue siendo útil: es la variante tal y como
        # la publican; solo se descartan los esquemas que no son HTTP.
        if urlparse(url).scheme and urlparse(url).scheme.lower() not in {"http", "https"}:
            pending = None
            continue
        if not url:
            pending = None
            continue
        key = _url_key(url)
        bandwidth = int(re.sub(r"\D", "", pending.get("bandwidth", "")) or 0)
        average = int(re.sub(r"\D", "", pending.get("avg-bandwidth", "")) or 0)
        resolution = pending.get("resolution", "").strip()
        if key not in seen:
            seen.add(key)
            variants.append(HlsVariant(
                url=url,
                bandwidth=bandwidth,
                average_bandwidth=average or bandwidth,
                resolution=resolution,
                codecs=pending.get("codecs", "").strip(),
                name=pending.get("name", "").strip(),
            ))
        pending = None
    variants.sort(key=lambda v: (_resolution_area(v.resolution), v.bandwidth, v.average_bandwidth),
                  reverse=True)
    return tuple(variants)


def inspect_hls_playlist(text: str, base_url: str = "") -> HlsPlaylist:
    """Clasifica el cuerpo de una playlist HLS y resume su contenido.

    Distingue master playlists (variantes), media playlists (segmentos) y listas sin
    contenido util. No se descargan los segmentos: solo se analiza la muestra leída.
    """
    if not text:
        return HlsPlaylist()
    sample = text.lstrip("\ufeff")
    if not sample.upper().lstrip().startswith("#EXTM3U"):
        return HlsPlaylist()
    variants = parse_hls_variants(sample, base_url)
    lines = [line.strip() for line in sample.splitlines()]
    upper_flags = {line.upper().split(":", 1)[0] for line in lines if line.startswith("#")}
    segment_count = sum(1 for line in lines if line.upper().startswith("#EXTINF:"))
    has_endlist = "#EXT-X-ENDLIST" in upper_flags
    duration_match = HLS_TARGET_DURATION_RE.search(sample)
    type_match = HLS_PLAYLIST_TYPE_RE.search(sample)
    encrypted = bool(
        re.search(r"#EXT-X-KEY\s*:\s*[^#\n]*METHOD=(?!NONE)", sample, re.I)
    )
    # `#EXT-X-MEDIA:TYPE=AUDIO,URI="…"`: pistas de audio/subtítulos del mismo canal.
    media_urls = tuple(
        _clean_url(match.group("attrs").split("uri=", 1)[-1].strip().strip('\"'), base_url)
        for match in HLS_MEDIA_RE.finditer(sample)
        if re.search(r"\bURI=\"", match.group("attrs"), re.I)
    )
    if variants:
        kind = "master"
    elif segment_count or "#EXT-X-BYTERANGE" in upper_flags or "#EXT-X-TARGETDURATION" in upper_flags:
        kind = "media"
    else:
        kind = "empty"
    is_live: bool | None = None
    if kind == "media":
        is_live = not has_endlist and (type_match is None or type_match.group(1).upper() == "EVENT")
    elif kind == "master":
        is_live = None
    return HlsPlaylist(
        kind=kind,
        variants=variants,
        media_urls=media_urls,
        segment_count=segment_count,
        target_duration=float(duration_match.group(1)) if duration_match else 0.0,
        is_live=is_live,
        encrypted=encrypted,
        # Sin `#EXT-X-ENDLIST` y con segmentos sueltos la muestra quedó cortada.
        truncated=not has_endlist and kind == "media",
    )


def _playlist_directive(value: str) -> tuple[str, str] | None:
    """Convierte `#EXTVLCOPT`/`#EXTHTTP`/`#KODIPROP` en una cabecera HTTP utilizable."""
    text = (value or "").strip()
    if not text:
        return None
    match = EXTVLCOPT_RE.match(text)
    if match:
        name, raw = match.group(1).casefold(), match.group(2).strip().strip('"')
        header = PLAYLIST_HEADER_KEYS.get(name)
        if header is None:
            return None
        return header, raw
    match = EXTHTTP_RE.match(text)
    if match:
        # `#EXTHTTP:{"cookie":"a=1","User-Agent":"x"}`: JSON o lista `k=v` suelta.
        payload = match.group(1).strip()
        pairs: dict[str, str] = {}
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            for key, item in parsed.items():
                if isinstance(item, (str, int, float)):
                    pairs[str(key)] = str(item)
        else:
            for part in payload.split("&"):
                if "=" in part:
                    key, item = part.split("=", 1)
                    pairs[key.strip()] = item.strip()
        if not pairs:
            return None
        headers = {}
        for key, item in pairs.items():
            normalized = PLAYLIST_HEADER_KEYS.get(key.strip().casefold(), key.strip().title())
            headers[normalized] = item
        return "EXTHTTP", json.dumps(headers, ensure_ascii=False)
    match = KODIPROP_RE.match(text)
    if match and "headers" in match.group(1).casefold():
        # `#KODIPROP:inputstream.adaptive.stream_headers=User-Agent=..&Referer=..`
        headers: dict[str, str] = {}
        for part in match.group(2).split("&"):
            if "=" not in part:
                continue
            key, item = part.split("=", 1)
            normalized = PLAYLIST_HEADER_KEYS.get(key.strip().casefold(), key.strip().title())
            headers[normalized] = item.strip()
        if not headers:
            return None
        return "EXTHTTP", json.dumps(headers, ensure_ascii=False)
    return None


def parse_stream_headers(directives: Iterable[str]) -> dict[str, str]:
    """Agrupación de cabeceras a partir de directivas de una lista M3U."""
    headers: dict[str, str] = {}
    for value in directives:
        parsed = _playlist_directive(value)
        if not parsed:
            continue
        name, item = parsed
        if name == "EXTHTTP":
            try:
                extra = json.loads(item)
            except (ValueError, TypeError):
                extra = {}
            if isinstance(extra, dict):
                for key, key_value in extra.items():
                    if key_value:
                        headers[str(key)] = str(key_value)
            continue
        if item:
            headers[name] = item
    return headers


def _name_from_url(url: str) -> str:
    """Nombre legible derivado del recurso (``espn-2-hd.m3u8`` → ``espn 2 hd``).

    Muchas listas publican entradas sin nombre; usar el archivo del stream evita
    perder el canal por quedarse en ``Unnamed channel``.
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    stem = path.rsplit("/", 1)[-1]
    stem = unquote(stem)
    stem = re.sub(r"\.(?:m3u8?|mpd|ts|mp4|key)$", "", stem, flags=re.I)
    stem = re.sub(r"[-_.+]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        return ""
    letters = re.sub(r"[^A-Za-z]", "", stem)
    # Identificadores opacos (hashes, tokens) no sirven como nombre de canal.
    if re.fullmatch(r"(?:[0-9A-Fa-f]{8,}|[A-Za-z0-9_-]{16,})", stem.replace(" ", "")):
        return ""
    if len(stem) >= 12 and (len(letters) < 4 or not re.search(r"[aeiouáéíóú]", letters, re.I)):
        return ""
    return stem


def _fix_mojibake(text: str) -> str:
    """Repara UTF-8 mal interpretado como Latin-1 (``SeÃ±al`` → ``Señal``)."""
    if not text or not re.search(r"[ÂÃÅ][\x80-\xbf]|â€", text):
        return text
    try:
        repaired = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    # Solo se acepta si reduce los caracteres extraños.
    if repaired and sum(1 for char in repaired if char in "ÂÃâ€") < sum(
        1 for char in text if char in "ÂÃâ€"
    ):
        return repaired
    return text


def _clean_entry_name(name: str) -> str:
    """Texto visible de una entrada: entidades HTML, control y mojibake resueltos."""
    text = unescape(name or "")
    text = "".join(char for char in text if char == "\t" or ord(char) >= 32)
    text = _fix_mojibake(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_m3u(text: str, *, deduplicate: bool = True, source: str = "",
              base_url: str = "") -> list[Channel]:
    lines = text.lstrip("\ufeff").splitlines()
    # A media playlist describes segments of one stream, not a list of TV channels.
    upper_lines = [line.strip().upper() for line in lines[:100]]
    if any(line.startswith(("#EXT-X-TARGETDURATION:", "#EXT-X-MEDIA-SEQUENCE:"))
           for line in upper_lines):
        return []
    # Un master playlist de un solo canal tampoco es una lista de canales: sus líneas
    # siguientes son variantes (mismas calidades), no emisoras distintas.
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in upper_lines) and not any(
        line.startswith("#EXTINF:") for line in upper_lines
    ):
        return []

    result: list[Channel] = []
    pending: tuple[str, dict[str, str], str] | None = None
    pending_headers: dict[str, str] = {}
    global_headers: dict[str, str] = {}
    current_group = ""
    directives: list[str] = []
    any_entry_seen = False
    seen: set[tuple[str, str]] = set()
    pending_raw: list[str] = []
    global_raw: list[str] = []

    def collect_raw(line: str) -> None:
        """Anota una directiva que no se convierte en cabecera y hay que reescribir.

        Son las que cambian el comportamiento del reproductor sin ser HTTP headers:
        `#EXT-X-KEY` (descifrado), un `#KODIPROP` de licencias o cualquier `#EXTVLCOPT`
        que no sea user-agent/referrer/cookie (p. ej. `http-proxy`).
        """
        nonlocal pending_raw, global_raw
        if any_entry_seen:
            if line not in pending_raw:
                pending_raw.append(line)
        elif line not in global_raw:
            global_raw.append(line)

    def collect_directives(line: str) -> None:
        """Guarda una directiva.

        Antes del primer `#EXTINF` se entiende como un bloque válido para toda la lista
        (así publican muchas listas su User-Agent/Referer común); a partir de la primera
        entrada, cada directiva afecta solo a la entrada siguiente y sustituye únicamente
        la clave que declara. Lo que no se traduce a cabeceras se conserva en crudo.
        """
        nonlocal directives, global_headers
        if any_entry_seen:
            directives.append(line)
        else:
            global_headers = {**global_headers, **parse_stream_headers([line])}
        if not parse_stream_headers([line]):
            collect_raw(line)

    def flush_headers() -> dict[str, str]:
        """Cabeceras pendientes de `#EXTVLCOPT`/`#EXTHTTP`, ya asignadas a la entrada."""
        nonlocal directives, pending_headers
        if directives:
            pending_headers = parse_stream_headers(directives)
            directives = []
        headers, pending_headers = pending_headers, {}
        return {**global_headers, **headers}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTINF:"):
            any_entry_seen = True
            name, attrs = _header_parts(line[len("#EXTINF:"):])
            clean = _clean_entry_name(name or attrs.get("tvg-name", ""))
            pending = (clean or "Unnamed channel", attrs,
                       attrs.get("group-title", "") or current_group)
            continue
        if upper.startswith("#EXTGRP:"):
            group = _clean_entry_name(line.split(":", 1)[1])
            if pending is not None:
                name, attrs, _ = pending
                pending = (name, attrs, group)
            else:
                # `#EXTGRP` suelto: se aplica a todas las entradas siguientes.
                current_group = group
            continue
        if upper.startswith(("#EXTVLCOPT:", "#EXTHTTP:", "#KODIPROP:")):
            collect_directives(line)
            continue
        # `#EXT-X-STREAM-INF` + URI son variantes de un mismo canal: se ignoran aquí y
        # se resuelven al comprobar el stream (ver `parse_hls_variants`).
        if upper.startswith("#EXT-X-STREAM-INF:"):
            continue
        # La clave de descifrado (y el byte-range) afectan a la entrada siguiente: sin
        # reescribirlas, la lista exportada apunta a un stream cifrado sin la clave.
        if upper.startswith(("#EXT-X-KEY:", "#EXT-X-BYTERANGE:")):
            collect_raw(line)
            continue
        if line.startswith("#") or pending is None:
            continue

        name, attrs, group = pending
        headers = flush_headers()
        entry_directives = list(global_raw)
        for extra in pending_raw:
            if extra not in entry_directives:
                entry_directives.append(extra)
        pending_raw = []
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

        if not name or name == "Unnamed channel":
            derived = _clean_entry_name(_name_from_url(line))
            if derived:
                name = derived
                attrs.setdefault("tvg-name", derived)

        key = (name.casefold(), line)
        if not deduplicate or key not in seen:
            category, language, country = classify_channel(name, group, attrs)
            logo = _clean_logo_url(attrs.get("tvg-logo", ""), base_url)
            result.append(Channel(
                name=name, url=line, group=group,
                tvg_id=attrs.get("tvg-id", ""), tvg_name=attrs.get("tvg-name", ""),
                logo=logo, language=language, country=country,
                category=category, source=source, attributes=attrs,
                headers=headers or None, directives=entry_directives,
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


def _unescape_markup(text: str) -> str:
    """Devuelve el documento con las URLs "escapadas" de JS/JSON ya legibles.

    Los reproductores escriben a menudo ``https:\\/\\/host\\/embed`` (slashes escapadas)
    o ``&#038;``/``\\u0026`` en los parámetros; sin desescapar no se pueden enlazar.
    """
    if not text:
        return ""
    if "\\" in text:
        text = ESCAPED_SLASH_RE.sub("/", text)
        if UNICODE_ESCAPE_RE.search(text):
            for escaped, char in UNICODE_ESCAPE_MAP.items():
                text = re.sub(re.escape(escaped), char, text, flags=re.I)
    if "&" in text:
        text = unescape(text)
    return text


def _is_stream_url(url: str) -> bool:
    """True para URLs que un reproductor puede abrir directamente.

    Un wrapper de reproductor (`/live/core.php?canal=…`, `/cvatt.html?get=…`) también
    contiene `/live/`, así que las extensiones de página se descartan primero.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = (parsed.query or "").lower()
    if path.endswith((".m3u8", ".m3u", ".mpd")):
        return True
    # El stream también puede venir en la query: `?url=https://h/x.m3u8`, `?file=…m3u8`.
    if re.search(r"(?:^|&|\\|=|%3d)[^&]*\.(?:m3u8|mpd)(?:&|$)", query):
        return True
    if path.endswith((".php", ".html", ".htm", ".json", ".js", ".css", ".jpg", ".jpeg",
                      ".png", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2", ".ttf",
                      ".mp4", ".webm", ".mp3", ".pdf", ".xml", ".txt")):
        return False
    if "/hls/" in path or "/live/" in path or "/live" == path.rstrip("/"):
        return True
    return path.endswith(("/playlist", "/stream", "/index", "/master", "/hls", "/dash"))


def _looks_like_player_url(url: str) -> bool:
    """Heurística de "aquí hay un reproductor", no un recurso estático ni un anuncio."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    path = parsed.path.lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css",
                      ".js", ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".mp3", ".pdf",
                      ".xml", ".txt", ".zip")):
        return False
    if _is_stream_url(url):
        return True
    if any(hint in parsed.netloc.lower() for hint in ("doubleclick", "googlesyndication",
                                                      "adsystem", "adservice", "analytics")):
        return False
    return path.endswith((".php", ".html", ".htm", ".json")) or any(
        hint in path for hint in PLAYER_URL_HINTS
    )


def _decode_base64_urls(text: str) -> list[str]:
    """URLs ocultas tras ``atob("…")``: muchos wrappers ofuscan el ``.m3u8`` así."""
    out: list[str] = []
    for match in BASE64_BLOB_RE.finditer(text):
        blob = re.sub(r"\s+", "", match.group(1))
        if len(blob) % 4:
            blob += "=" * (4 - len(blob) % 4)
        if len(blob) > 4096:
            continue
        try:
            decoded = base64.b64decode(blob, validate=False)
        except (ValueError, UnicodeError):
            continue
        if not decoded:
            continue
        value = decoded.decode("utf-8", errors="replace").strip().strip("\"'")
        if "://" in value or ".m3u8" in value.lower() or ".mpd" in value.lower():
            out.append(value)
    return out


def _decode_b64_value(value: str) -> str:
    """Devuelve la URL oculta tras un valor base64 de un atributo o clave JS."""
    text = (value or "").strip()
    if len(text) < 16 or len(text) > 4096 or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
        return ""
    padded = re.sub(r"\s+", "", text)
    if len(padded) % 4:
        padded += "=" * (4 - len(padded) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return ""
    candidate = decoded.strip().strip("\"'")
    if "://" in candidate or ".m3u8" in candidate.lower() or ".mpd" in candidate.lower():
        return candidate
    return ""


def _player_hint_values(text: str) -> list[str]:
    """Valores donde los reproductores guardan el stream: claves JS, ``data-*``, metas."""
    out: list[str] = []
    for pattern in (JS_STREAM_KEY_RE, DATA_ATTR_RE, MEDIA_TAG_SRC_RE, META_STREAM_RE):
        for value in pattern.findall(text):
            if isinstance(value, tuple):
                value = next((item for item in value if item), "")
            value = (value or "").strip()
            if value:
                out.append(value)
                decoded = _decode_b64_value(value)
                if decoded:
                    out.append(decoded)
    out.extend(_decode_base64_urls(text))
    return out


def _inline_script_blobs(text: str) -> list[str]:
    """Fragmentos de HTML construidos en JS (``document.write('<iframe …>')``)."""
    out: list[str] = []
    for match in DOCUMENT_WRITE_RE.finditer(text):
        blob = match.group(1)
        if len(blob) > 2048:
            continue
        out.append(JS_STRING_UNESCAPE_RE.sub(lambda found: found.group(0)[-1], blob))
        if len(out) >= 8:
            break
    return out


def _stream_urls_from_document(html: str, base: str) -> list[str]:
    """URLs reproducibles (HLS/MPD) presentes en el HTML o dentro de su JavaScript."""
    if not html:
        return []
    text = _unescape_markup(html)
    blobs = [text] + [_unescape_markup(blob) for blob in _inline_script_blobs(text)]
    raw: list[str] = []
    for blob in blobs:
        raw += _urls_from_html(blob, base)
        raw += JS_STRING_RE.findall(blob)
        raw += _player_hint_values(blob)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        url = _clean_url(item, base)
        if not _is_stream_url(url):
            continue
        key = _url_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _player_links_from_document(html: str, base: str) -> tuple[list[str], list[str]]:
    """Devuelve ``(iframes, urls_de_reproductor_en_js)``.

    Los iframes se aceptan aunque sean de otro dominio (son embeds explícitos); las
    URLs que aparecen dentro del JavaScript solo se siguen si son del propio sitio o
    de los dominios declarados en la fuente.
    """
    if not html:
        return [], []
    text = _unescape_markup(html)
    iframes: list[str] = []
    for value in IFRAME_SRC_RE.findall(text):
        url = _clean_url(value, base)
        if _looks_like_player_url(url):
            iframes.append(url)
    # `data-iframe="…"` / `data-embed="…"` son embeds explícitos del propio sitio.
    for value in EMBED_ATTR_RE.findall(text):
        url = _clean_url(value, base)
        if _looks_like_player_url(url):
            iframes.append(url)
    js_urls: list[str] = []
    for value in JS_STRING_RE.findall(text):
        url = _clean_url(value, base)
        if not _looks_like_player_url(url):
            continue
        # Los fragmentos que el JS completa en el navegador (`"/cvatt.html?get="`)
        # no son URLs utilizables: se reconstruyen en `_player_urls_from_config`.
        if url.rstrip("/").endswith(("=", "&", "?")):
            continue
        if urlparse(url).netloc.lower() == urlparse(base).netloc.lower():
            js_urls.append(url)
    return _unique_urls(iframes), _unique_urls(js_urls)


def _script_urls_from_document(html: str, base: str,
                               allowed_hosts: set[str]) -> list[str]:
    """Find same-site (or explicitly allowed) scripts that may publish stream data."""
    text = _unescape_markup(html)
    found: list[str] = []
    for match in SCRIPT_SRC_RE.finditer(text):
        url = _clean_url(match.group(2), base)
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if parsed.scheme.lower() not in {"http", "https"} or host not in allowed_hosts:
            continue
        if any(hint in host for hint in ("doubleclick", "googlesyndication", "googletagmanager")):
            continue
        if url not in found:
            found.append(url)
        if len(found) >= MAX_PLAYER_CANDIDATES:
            break
    return found


def _youtube_embed_urls(html: str) -> list[str]:
    """Convierte los embeds de YouTube (``/embed/ID``, ``/live/ID``) en URLs estables."""
    text = _unescape_markup(html)
    out: list[str] = []
    for match in YOUTUBE_EMBED_RE.finditer(text):
        video_id = match.group(1)
        url = f"https://www.youtube.com/embed/{video_id}"
        if url not in out:
            out.append(url)
        if len(out) >= 4:
            break
    return out


def _json_config_urls(html: str, base: str) -> list[str]:
    """Endpoints ``*.json`` citados en el JavaScript (por ejemplo ``/html/cv.json``)."""
    text = _unescape_markup(html)
    out: list[str] = []
    for value in JSON_ENDPOINT_RE.findall(text):
        url = _clean_url(value, base)
        if urlparse(url).scheme.lower() not in {"http", "https"}:
            continue
        if url not in out:
            out.append(url)
        if len(out) >= MAX_CONFIG_ENDPOINTS:
            break
    return out


def _without_youtube(urls: Iterable[str]) -> list[str]:
    """Los embeds de YouTube se extraen aparte; no hace falta descargarlos."""
    return [
        url for url in urls
        if urlparse(url).netloc.lower() not in YOUTUBE_HOSTS
    ]


def _unique_urls(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _url_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _strings_in_json(payload: object) -> list[str]:
    out: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            out.append(node)

    walk(payload)
    return out


def _hosts_in_json(payload: object) -> list[str]:
    """Dominios publicados en un JSON de configuración (``{"urls": ["host.com"]}``)."""
    hosts: list[str] = []
    for value in _strings_in_json(payload):
        candidate = value.strip().strip("/").lower()
        if not candidate or "://" in candidate or "/" in candidate or " " in candidate:
            continue
        if not BASIC_JSON_NAME_RE.match(candidate):
            continue
        if candidate.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".css", ".js")):
            continue
        if candidate not in hosts:
            hosts.append(candidate)
    return hosts[:4]


def _absolute_urls_in_json(payload: object) -> list[str]:
    return _unique_urls(
        value for value in _strings_in_json(payload)
        if value.strip().lower().startswith(("http://", "https://"))
    )


def _player_path_fragments(html: str, document_url: str) -> list[str]:
    """Rutas de reproductor que el wrapper arma en JS: ``"/cvatt.html?get=" + token``.

    Cuando el fragmento termina en ``=`` se completa con el valor del mismo parámetro
    de la propia URL (o con el primer valor disponible), que es como el navegador
    reconstruye el enlace del reproductor.
    """
    text = _unescape_markup(html)
    query_values = {
        key: value for key, value in
        (pair.split("=", 1) if "=" in pair else (pair, "") for pair in urlparse(document_url).query.split("&"))
        if key
    }
    out: list[str] = []
    for value in JS_STRING_RE.findall(text):
        if not value.startswith("/") or value.startswith("//"):
            continue
        resolved = _clean_url(value, document_url)
        if urlparse(resolved).path.lower().endswith(".json"):
            # Los JSON de configuración se resuelven aparte, no son el reproductor.
            continue
        if not _looks_like_player_url(resolved):
            continue
        fragment = value
        match = re.search(r"[?&]([A-Za-z0-9_-]+)=$", fragment)
        if match:
            name = match.group(1)
            token = query_values.get(name) or next(
                (v for v in query_values.values() if v), ""
            )
            fragment = fragment + token
        if fragment not in out:
            out.append(fragment)
    return out[:4]


def _player_urls_from_config(payload: object, referrer_html: str,
                             referrer_url: str) -> list[str]:
    """Reproduce la URL del reproductor que el wrapper construye con su JSON de config."""
    candidates = list(_absolute_urls_in_json(payload))
    paths = _player_path_fragments(referrer_html, referrer_url)
    for host in _hosts_in_json(payload):
        for path in paths:
            candidates.append(f"https://{host}{path}")
    return [
        url for url in _unique_urls(candidates)
        if _looks_like_player_url(url) or _is_stream_url(url)
    ]


def _country_from_source_path(source: SourceConfig, page: str) -> str:
    prefix = source.country_path_prefix
    if not prefix:
        return ""
    path = urlparse(page).path
    if not path.casefold().startswith(prefix.casefold()):
        return ""
    token = path[len(prefix):].split("/", 1)[0]
    return _code(token, COUNTRY_CODES, upper=True)


def _source_country_hint(source: SourceConfig) -> str:
    return _code(source.country_hint, COUNTRY_CODES, upper=True)


def _apply_source_country(channel: Channel, source: SourceConfig, page: str = "",
                          *, path_overrides: bool = False) -> None:
    path_country = _country_from_source_path(source, page) if page else ""
    hint = _source_country_hint(source)
    if path_country and path_overrides:
        channel.country = path_country
    elif channel.country == "unknown":
        channel.country = path_country or hint or "unknown"


def _source_headers(source: SourceConfig) -> dict[str, str]:
    """Cabeceras declaradas en la fuente (``headers=("Referer: https://…",)``)."""
    return _parse_header_pairs(source.headers)


def _player_headers(source: SourceConfig, page: str) -> dict[str, str]:
    """Cabeceras con las que hay que pedir el stream de un sitio rastreado.

    Estos CDNs suelen bloquear el *hotlinking*: el `.m3u8` responde 403 salvo que la
    petición venga del propio reproductor (User-Agent de navegador + Referer de la
    página del canal).
    """
    headers = {
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Referer": page,
    }
    headers.update(_source_headers(source))
    return headers


def _site_channel(name: str, url: str, source: SourceConfig, page: str, logo: str,
                  stream_type: str = STREAM_TYPE_HLS,
                  headers: dict[str, str] | None = None) -> Channel:
    category, language, country = classify_channel(name, source.name)
    path_country = _country_from_source_path(source, page)
    if path_country:
        # A country-specific route is more reliable than a country name embedded
        # in a channel brand (e.g. "France 24" appearing in a US market list).
        country = path_country
    elif country == "unknown":
        country = _source_country_hint(source) or "unknown"
    stream_headers = _player_headers(source, page) if stream_type == STREAM_TYPE_HLS else {}
    stream_headers.update(headers or {})
    return Channel(
        name=name, url=url, group=source.name, logo=logo,
        language=language, country=country, category=category,
        source=source.name, source_url=page, stream_type=stream_type,
        headers=stream_headers or None,
    )


def _channels_from_json_payload(payload: object, source: SourceConfig,
                                source_url: str, source_page: str = "") -> list[Channel]:
    """Parse channel records in public JSON feeds (name/country/sources schema)."""
    channels: list[Channel] = []

    def record_urls(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [item for item in value if isinstance(item, str)]
        return []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("channel_name") or "").strip()
            sources = node.get("sources")
            source_map = sources if isinstance(sources, dict) else {}
            stream_urls = record_urls(source_map.get("streams"))
            stream_urls.extend(record_urls(node.get("stream_url")))
            youtube_urls = record_urls(source_map.get("youtube"))
            country_value = str(
                node.get("country") or node.get("country_code") or node.get("countryCode") or ""
            )
            languages = node.get("languages") or node.get("language") or ""
            if isinstance(languages, (list, tuple)):
                language_value = ",".join(str(value) for value in languages if value)
            else:
                language_value = str(languages)
            category_value = str(node.get("category") or node.get("group") or "")
            logo = _clean_logo_url(
                str(node.get("logo") or node.get("logo_url") or node.get("image") or ""),
                source_url,
            )
            attrs = {
                "tvg-country": country_value,
                "tvg-language": language_value,
                "tvg-id": str(node.get("tvg_id") or node.get("tvg-id") or node.get("nanoid") or ""),
                "tvg-name": name,
            }
            category, language, country = classify_channel(name, category_value, attrs)
            if category_value.casefold() in CATEGORY_RULES:
                category = category_value.casefold()
            candidates = [
                *((url, STREAM_TYPE_HLS) for url in stream_urls),
                *((url, STREAM_TYPE_YOUTUBE) for url in youtube_urls),
            ]
            if name:
                for value, stream_type in candidates:
                    url = _clean_url(value, source_url)
                    parsed = urlparse(url)
                    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                        continue
                    if stream_type == STREAM_TYPE_HLS and not _is_stream_url(url):
                        continue
                    if stream_type == STREAM_TYPE_YOUTUBE and parsed.netloc.lower() not in YOUTUBE_HOSTS:
                        continue
                    channel = Channel(
                        name=name, url=url, group=category_value or source.name,
                        tvg_id=attrs["tvg-id"], tvg_name=name, logo=logo,
                        language=language, country=country or "unknown", category=category,
                        source=source.name, source_url=source_page or source_url,
                        stream_type=stream_type, attributes=attrs,
                    )
                    _apply_source_country(channel, source, source_page or source_url)
                    channels.append(channel)
            for key, value in node.items():
                if key != "sources":
                    visit(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                visit(value)

    visit(payload)
    return _dedupe(channels)


def _page_title(html: str, fallback: str, *, clean: bool = True) -> str:
    for tag in ("h1", "title"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.I | re.S)
        if match:
            raw_text = " ".join(unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())
            text = clean_channel_name(raw_text) if clean else raw_text
            comparable = clean_channel_name(text)
            if text and _canonical_channel_key(comparable) not in GENERIC_CHANNEL_NAMES:
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


def _apply_socket_timeout(timeout: int) -> None:
    """Bound every socket created afterwards, including body reads.

    ``urlopen(timeout=...)`` only covers connection and header reads; without this a
    slow server could keep ``response.read()`` blocked forever and hang the whole run.
    """
    try:
        socket.setdefaulttimeout(max(15, int(timeout) * 2))
    except (TypeError, ValueError):
        socket.setdefaulttimeout(30)


def _decode_body(data: bytes, content_encoding: str) -> bytes:
    """Decompress gzip/deflate bodies, which urllib never does automatically."""
    encoding = (content_encoding or "").strip().lower()
    if encoding in {"", "identity"}:
        return data
    if encoding == "gzip":
        return gzip.decompress(data)
    if encoding == "deflate":
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(data, -zlib.MAX_WBITS)
    if encoding == "br":
        try:
            import brotli  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ValueError("unsupported Content-Encoding: br") from exc
        return brotli.decompress(data)
    raise ValueError(f"unsupported Content-Encoding: {encoding}")


def _decode_text(data: bytes, charset: str) -> str:
    """Decodifica con la codificación detectada y repara el mojibake más común."""
    candidates = [value for value in (charset, "utf-8", "windows-1252") if value]
    for candidate in dict.fromkeys(candidates):
        try:
            return _fix_mojibake(data.decode(candidate))
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _sniff_charset(data: bytes, declared: str) -> str:
    """Codificación real del documento cuando el servidor no la declara."""
    declared = (declared or "").strip().lower()
    if declared:
        return declared
    match = re.search(
        rb"""<meta[^>]+?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]{3,30})""", data[:4096], re.I,
    )
    if match:
        candidate = match.group(1).decode("ascii", errors="ignore").strip().lower()
        try:
            codecs.lookup(candidate)
        except LookupError:
            return "utf-8"
        return candidate
    return "utf-8"


def _request_headers(extra: dict[str, str] | None) -> dict[str, str]:
    headers = dict(BROWSER_HEADERS)
    headers["Accept-Encoding"] = "gzip, deflate" + (", br" if _BROTLI_AVAILABLE else "")
    for key, value in (extra or {}).items():
        if key and value:
            headers[str(key).strip()] = str(value).strip()
    return headers


def _parse_header_pairs(values: Iterable[str]) -> dict[str, str]:
    """Convierte ``("Key: Value", …)`` en un diccionario de cabeceras."""
    headers: dict[str, str] = {}
    for value in values:
        if not value or ":" not in value:
            continue
        key, item = value.split(":", 1)
        if key.strip():
            headers[key.strip()] = item.strip()
    return headers


def _throttle(host: str, min_interval: float) -> None:
    """Respeta un intervalo mínimo entre peticiones al mismo host (cortesía)."""
    if min_interval <= 0 or not host:
        return
    with _HOST_LOCK:
        now = time.monotonic()
        wait = _HOST_LAST.get(host, 0.0) + min_interval - now
        _HOST_LAST[host] = max(_HOST_LAST.get(host, 0.0), now) + min_interval
    if wait > 0:
        time.sleep(min(wait, 5.0))


def _fetch_text_once(url: str, timeout: int, max_bytes: int,
                     headers: dict[str, str] | None = None) -> str:
    _apply_socket_timeout(timeout)
    _throttle(urlparse(url).netloc.lower(), POLITE_DELAY)
    req = Request(url, headers=_request_headers(headers))
    with urlopen(req, timeout=max(1, timeout)) as response:
        raw = response.read(max_bytes)
        content_encoding = response.headers.get("Content-Encoding", "")
        charset = response.headers.get_content_charset() or ""
    data = _decode_body(raw, content_encoding)
    text = _decode_text(data, _sniff_charset(data, charset))
    if not text.lstrip("\ufeff \r\n\t") and raw:
        # Algunas listas se publican con una codificación distinta de la declarada.
        text = data.decode("latin-1", errors="replace")
    return text


def _fetch_text(url: str, timeout: int = 20, max_bytes: int = DEFAULT_MAX_BYTES,
                retries: int = 2, headers: dict[str, str] | None = None,
                use_cache: bool = False) -> str:
    """Download text with bounded reads, compression support and a couple of retries.

    Responses larger than ``max_bytes`` are truncated instead of rejected so a huge
    playlist (iptv-org publishes tens of MB) still yields channels.

    ``use_cache`` reutiliza el documento dentro de la misma ejecución: al rastrear un
    sitio, decenas de páginas comparten el mismo JSON de configuración y el mismo
    wrapper, y los errores se recuerdan brevemente para no castigar un endpoint caído.
    Se activa solo en el rastreo de reproductores; las listas se descargan siempre.
    """
    cache_key = f"{url}|{sorted((headers or {}).items())}"
    now = time.monotonic()
    if use_cache:
        with _CACHE_LOCK:
            hit = _FETCH_CACHE.get(cache_key)
            if hit and hit[0] > now:
                if hit[1] is None:
                    raise CachedFetchError(hit[2])
                return hit[1]

    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        if attempt:
            delay = min(2 ** attempt, 5) + random.uniform(0, 0.4)
            retry_after = _retry_after_seconds(last_error)
            if retry_after is not None:
                delay = min(max(delay, retry_after), 10.0)
            time.sleep(delay)
        try:
            text = _fetch_text_once(url, timeout, max_bytes, headers)
        except HTTPError as exc:
            exc.close()
            last_error = exc
            if exc.code in RETRY_STATUS and attempt < max(0, retries):
                continue
            break
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < max(0, retries):
                continue
            break
        else:
            if use_cache and len(text) <= MAX_CACHED_BYTES:
                with _CACHE_LOCK:
                    _FETCH_CACHE[cache_key] = (time.monotonic() + FETCH_CACHE_TTL, text, "")
                    while len(_FETCH_CACHE) > FETCH_CACHE_SIZE:
                        _FETCH_CACHE.popitem(last=False)
            return text
    error = last_error if last_error else RuntimeError(f"could not download {url}")
    if use_cache:
        with _CACHE_LOCK:
            _FETCH_CACHE[cache_key] = (
                time.monotonic() + FAILURE_CACHE_TTL, None, str(error)[:240],
            )
            while len(_FETCH_CACHE) > FETCH_CACHE_SIZE:
                _FETCH_CACHE.popitem(last=False)
    raise error


def _retry_after_seconds(error: Exception | None) -> float | None:
    """`Retry-After` (segundos o fecha HTTP) limitado a 10 s."""
    headers = getattr(error, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if not raw:
        return None
    text = str(raw).strip()
    if text.isdigit():
        return min(float(text), 10.0)
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    delay = when.timestamp() - time.time()
    return min(max(delay, 0.0), 10.0)


def clear_fetch_cache() -> None:
    """Vacía la caché de descargas (la usan las pruebas y las ejecuciones largas)."""
    with _CACHE_LOCK:
        _FETCH_CACHE.clear()


def _looks_like_m3u(text: str) -> bool:
    sample = text.lstrip("\ufeff \r\n")
    return sample.startswith("#EXTM3U") or "#EXTINF:" in sample[:100000]


def _is_youtube_embed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() in YOUTUBE_HOSTS and "/embed/" in parsed.path


def _check_youtube_embed(url: str, timeout: int,
                         headers: dict[str, str] | None = None) -> StreamCheck:
    """Comprueba un embed de YouTube: HTTP 200 y emisión en vivo detectada.

    Muchas fuentes (Teleonline TV, TV en Vivo, TV Libre Online) solo publican embeds
    de YouTube; se consideran activos únicamente cuando la página del embed declara
    una emisión en vivo, para no sincronizar vídeos grabados como si fueran canales.
    """
    _apply_socket_timeout(timeout)
    request_headers = {
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": BROWSER_HEADERS["Accept-Language"],
    }
    for key, value in (headers or {}).items():
        if value and key.casefold() != "user-agent":
            request_headers[key] = value
    try:
        with urlopen(Request(url, headers=request_headers), timeout=max(1, timeout)) as response:
            status = int(response.status)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            final_url = response.geturl()
            sample = response.read(512 * 1024)
    except HTTPError as exc:
        content_type = exc.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() \
            if exc.headers else ""
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

    haystack = sample.decode("utf-8", errors="replace").casefold()
    if any(marker in haystack for marker in YOUTUBE_LIVE_MARKERS):
        return StreamCheck(
            "http_ok", status, content_type, final_url,
            "YouTube: emisión en vivo detectada (no se probaron segmentos)",
        )
    return StreamCheck(
        "not_live", status, content_type, final_url,
        "YouTube respondió, pero el embed no declara una emisión en vivo",
    )


def _stream_request_headers(headers: dict[str, str] | None = None, *,
                            with_range: bool = True) -> dict[str, str]:
    """Cabeceras de la sonda: UA/Referer de la lista si los hay, y un Range corto."""
    merged = {
        "User-Agent": (headers or {}).get("User-Agent") or USER_AGENT,
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
    }
    for key, value in (headers or {}).items():
        if value and key.casefold() != "user-agent":
            merged[key] = value
    if with_range:
        merged["Range"] = f"bytes=0-{STREAM_SAMPLE_BYTES - 1}"
    return merged


def _open_stream_sample(url: str, timeout: int,
                        headers: dict[str, str] | None = None, *,
                        with_range: bool = True) -> tuple[int, str, str, bytes]:
    """Descarga solo la cabecera de un recurso y devuelve ``(status, tipo, url, muestra)``."""
    request = Request(url, headers=_stream_request_headers(headers, with_range=with_range))
    with urlopen(request, timeout=max(1, timeout)) as response:
        status = int(response.status)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        return status, content_type, response.geturl(), response.read(STREAM_SAMPLE_BYTES)


def _describe_hls(playlist: HlsPlaylist) -> str:
    """Resumen legible de una playlist HLS inspeccionada."""
    if playlist.kind == "media":
        parts = [f"{playlist.segment_count} segmentos"]
        if playlist.target_duration:
            parts.append(f"{playlist.target_duration:g}s por segmento")
        parts.append("en vivo" if playlist.is_live else "VOD")
        if playlist.encrypted:
            parts.append("cifrada con AES-128")
        return "Playlist HLS con " + ", ".join(parts)
    if playlist.kind == "master":
        best = playlist.variants[0]
        label = best.resolution or best.name or f"{best.bandwidth or '?'} bps"
        return f"master playlist con {len(playlist.variants)} variantes (mejor: {label})"
    return ""


def _resolve_hls_variants(playlist: HlsPlaylist, timeout: int,
                          headers: dict[str, str] | None
                          ) -> tuple[str, str, int, bool | None, str] | None:
    """Cambia un *master playlist* por la media playlist jugable de mejor calidad.

    Es el paso que falta en muchas listas: el ``.m3u8`` publicado solo indexa
    calidades y el reproductor elige una. Aquí se prueban las mejores variantes y se
    devuelve ``(url, resolución, segmentos, en_vivo, detalle)`` de la primera que
    responde como playlist de segmentos.
    """
    if not playlist.variants:
        return None
    failures: list[str] = []
    for variant in playlist.variants[:MAX_VARIANT_PROBES]:
        label = variant.resolution or variant.name or f"{variant.bandwidth or '?'} bps"
        probe = _probe_stream(variant.url, timeout, headers)
        if isinstance(probe, StreamCheck):
            failures.append(f"{label}: {(probe.detail or probe.status)[:80]}")
            continue
        status, _content_type, final_url, sample = probe
        if not 200 <= status < 300:
            failures.append(f"{label}: HTTP {status}")
            continue
        info = inspect_hls_playlist(sample.decode("utf-8", errors="replace"), final_url)
        if info.kind == "master":
            # Variante que apunta a otro master: se usa su mejor URI, sin más peticiones.
            nested = info.variants[0] if info.variants else None
            return (
                (nested.url if nested else variant.url),
                (nested.resolution if nested else variant.resolution),
                0, None,
                f"HLS master anidado: {label} → "
                f"{(nested.resolution if nested else 'variante sin comprobar')}",
            )
        detail = _describe_hls(info) or "playlist sin segmentos en la muestra"
        if failures:
            detail += f"; variantes descartadas: {len(failures)}"
        label = f" ({variant.resolution})" if variant.resolution else ""
        return (final_url or variant.url, variant.resolution, info.segment_count,
                info.is_live, f"HLS master{label}: {detail}")
    best = playlist.variants[0]
    note = f" ({failures[0]})" if failures else ""
    return (
        "", best.resolution, 0, None,
        f"HLS master con {len(playlist.variants)} variantes; ninguna confirmó "
        f"segmentos{note}",
    )


def _http_error_check(exc: HTTPError, url: str) -> StreamCheck:
    """Clasifica un HTTPError como ``restricted`` o ``http_error``."""
    content_type = ""
    headers = getattr(exc, "headers", None)
    if headers:
        content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    state = "restricted" if exc.code in {401, 403, 407, 451} else "http_error"
    try:
        final_url = exc.geturl()
    except (AttributeError, TypeError):
        final_url = url
    exc.close()
    return StreamCheck(state, exc.code, content_type, final_url, f"HTTP {exc.code}")


def _probe_stream(url: str, timeout: int, headers: dict[str, str] | None, *,
                  with_range: bool = True,
                  ) -> tuple[int, str, str, bytes] | StreamCheck:
    """Sondea un stream y devuelve la muestra o un ``StreamCheck`` de error."""
    try:
        return _open_stream_sample(url, timeout, headers, with_range=with_range)
    except HTTPError as exc:
        if exc.code in {400, 405, 416, 501} and with_range:
            # Servidor que no acepta `Range`: se reintenta sin esa cabecera.
            exc.close()
            return _probe_stream(url, timeout, headers, with_range=False)
        return _http_error_check(exc, url)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return StreamCheck("unreachable", detail=str(reason)[:240])


def check_stream(url: str, timeout: int = 10,
                 headers: dict[str, str] | None = None,
                 resolve_variants: bool = True) -> StreamCheck:
    """Comprueba un rango HTTP pequeño y analiza el cuerpo de la playlist HLS.

    Además de la accesibilidad: si la respuesta es un *master playlist* se resuelve la
    variante jugable (``media_url``); si es una media playlist se cuentan los segmentos
    para distinguir un stream vivo de un ``.m3u8`` vacío o de una página HTML. Las
    cabeceras ``User-Agent``/``Referer``/``Cookie`` declaradas en la lista
    (``#EXTVLCOPT``) se reenvían, que es la diferencia entre un 403 y un stream OK.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return StreamCheck("unsupported", detail="Only absolute HTTP(S) URLs can be checked")

    if _is_youtube_embed(url):
        return _check_youtube_embed(url, timeout, headers)

    _apply_socket_timeout(timeout)
    probe = _probe_stream(url, timeout, headers)
    if isinstance(probe, StreamCheck):
        # Un 403 sin cabeceras propias suele ser protección anti-hotlink: se prueba
        # una última vez con el User-Agent de navegador antes de descartar el canal.
        if probe.status == "restricted" and "User-Agent" not in (headers or {}):
            retry = _probe_stream(url, timeout, {
                **(headers or {}),
                "User-Agent": BROWSER_HEADERS["User-Agent"],
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            })
            if not isinstance(retry, StreamCheck):
                probe = retry
        if isinstance(probe, StreamCheck):
            return probe
    status, content_type, final_url, sample = probe

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

    playlist = inspect_hls_playlist(sample.decode("utf-8", errors="replace"), final_url)
    if playlist.kind == "master":
        resolved = _resolve_hls_variants(playlist, timeout, headers) if resolve_variants else None
        if resolved is None:
            best = playlist.variants[0] if playlist.variants else None
            return StreamCheck(
                "http_ok", status, content_type, final_url, _describe_hls(playlist),
                "master", best.url if best else "", best.resolution if best else "",
            )
        media_url, resolution, segments, is_live, detail = resolved
        return StreamCheck(
            "http_ok", status, content_type, final_url, detail,
            "master", media_url, resolution, segments, is_live,
        )
    if playlist.kind == "media":
        return StreamCheck(
            "http_ok", status, content_type, final_url, _describe_hls(playlist),
            "media", "", "", playlist.segment_count, playlist.is_live,
        )
    if playlist.kind == "empty":
        return StreamCheck(
            "http_ok", status, content_type, final_url,
            "Playlist header detected; segments were not tested", "playlist",
        )
    detail = (
        "Playlist header detected; segments were not tested"
        if is_playlist else "HTTP response received; playback was not tested"
    )
    return StreamCheck("http_ok", status, content_type, final_url, detail)


def _remaining_seconds(deadline: float | None) -> float | None:
    """Seconds left before ``deadline`` (``time.monotonic`` based), None when unbounded."""
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def check_streams(channels: Iterable[Channel], timeout: int = 10,
                  workers: int = 4,
                  deadline: float | None = None,
                  resolve_variants: bool = True) -> dict[str, StreamCheck]:
    """Comprueba las URLs de unos canales en paralelo, con sus cabeceras de lista."""
    headers_by_url: dict[str, dict[str, str]] = {}
    urls: list[str] = []
    for channel in channels:
        if not channel.url:
            continue
        key = _url_key(channel.url)
        if key not in headers_by_url and channel.headers:
            headers_by_url[key] = dict(channel.headers)
        elif channel.headers:
            merged = dict(headers_by_url.get(key) or {})
            merged.update(channel.headers)
            headers_by_url[key] = merged
        if channel.url not in urls:
            urls.append(channel.url)
    if not urls:
        return {}

    results: dict[str, StreamCheck] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls))))
    try:
        futures = {
            pool.submit(
                check_stream, url, timeout, headers_by_url.get(_url_key(url)), resolve_variants,
            ): url
            for url in urls
        }
        waiter = as_completed(futures, timeout=_remaining_seconds(deadline)) \
            if deadline is not None else as_completed(futures)
        try:
            for future in waiter:
                url = futures[future]
                try:
                    results[url] = future.result()
                except Exception as exc:
                    results[url] = StreamCheck("error", detail=str(exc)[:240])
        except FuturesTimeout:
            for future, url in futures.items():
                if not future.done():
                    future.cancel()
                    results[url] = StreamCheck(
                        "timeout", detail="comprobación cancelada: se agotó el tiempo",
                    )
    finally:
        # Nunca bloquear el proceso esperando hebras que se quedaron sin presupuesto.
        pool.shutdown(wait=False, cancel_futures=True)
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
    if incoming.headers:
        if existing.headers is None:
            existing.headers = dict(incoming.headers)
        else:
            for key_name, value in incoming.headers.items():
                if value and not existing.headers.get(key_name):
                    existing.headers[key_name] = value
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
    # Una respuesta 200 no basta: se puntúa por encima el `.m3u8` que de verdad
    # declara variantes o segmentos, y se penaliza el que devuelve HTML.
    playlist_rank = 0
    if check is not None:
        if check.playlist_kind in {"media", "master"}:
            playlist_rank = 2
        elif check.status == "invalid_playlist":
            playlist_rank = -2
    has_headers = 1 if channel.headers else 0
    canonical = _canonical_channel_key(channel.name)
    non_generic = 1 if (canonical and canonical not in GENERIC_CHANNEL_NAMES) else 0
    has_category = 1 if channel.category != "other" else 0
    has_country = 1 if len(channel.country) == 2 else 0
    has_language = 1 if channel.language != "unknown" else 0
    preferred_lang = 1 if channel.language in {"es", "en", "pt"} else 0
    has_logo = 1 if bool(channel.logo) else 0
    has_tvg_id = 1 if bool(channel.tvg_id) else 0
    is_https = 1 if channel.url.lower().startswith("https://") else 0
    playable = 1 if (".m3u8" in channel.url.lower()
                     or channel.stream_type == STREAM_TYPE_YOUTUBE) else 0
    return (
        check_rank,
        playlist_rank,
        non_generic,
        has_category + has_country + has_language + has_logo + has_tvg_id + has_headers,
        preferred_lang,
        is_https + playable,
        playable,
        has_logo,
        has_country,
        has_language,
    )


def _source_bucket(channel: Channel) -> str:
    """Fuente "principal" de un canal, para repartir el cupo entre fuentes distintas."""
    return (channel.source.split(",")[0].strip().casefold() or "unknown")


def dedupe_unique_channels(
    channels: Iterable[Channel],
    *,
    limit: int | None = None,
    stream_checks: dict[str, StreamCheck] | None = None,
    only_http_ok: bool = False,
    diversify: bool = True,
    exclude_keys: tuple[set[str], set[str], set[str]] | None = None,
    max_per_source: int = 0,
    require_playlist: bool = False,
) -> list[Channel]:
    """Select unique channels without repeating name/slug, tvg_id, URL or final redirect URL.

    When ``limit`` is positive, channels are ranked by quality and interleaved across
    categories so the resulting list is diverse and free of duplicates.
    ``require_playlist`` descarta lo que responde 200 pero no es una playlist HLS.
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
        if require_playlist and (
            check is None
            or check.status != "http_ok"
            or (check.playlist_kind not in {"media", "master", "playlist"}
                and ".m3u8" not in channel.url.lower()
                and channel.stream_type != STREAM_TYPE_YOUTUBE)
        ):
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

        media_url_key = _url_key(check.media_url) if (check and check.media_url) else ""
        if media_url_key and media_url_key in seen_urls:
            # Otra URL que apunta a la misma playlist resuelta: mismo stream real.
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
            stream_type=channel.stream_type,
            attributes=dict(channel.attributes) if channel.attributes else None,
            headers=dict(channel.headers) if channel.headers else None,
        )
        seen_canonical[canonical] = normalized_channel
        seen_slugs.add(slug)
        seen_names.add(canonical)
        seen_urls.add(url_key)
        if final_url_key:
            seen_urls.add(final_url_key)
        if media_url_key:
            seen_urls.add(media_url_key)
        if tvg_key:
            seen_tvg_ids.add(tvg_key)
        unique.append(normalized_channel)

    if not diversify and max_per_source <= 0:
        unique.sort(key=lambda x: (x.category, x.language, x.name.casefold(), x.url))
        return unique[:limit] if (limit and limit > 0) else unique

    by_category: dict[str, list[Channel]] = defaultdict(list)
    for item in unique:
        by_category[item.category].append(item)

    ordered_categories = [cat for cat in CATEGORY_PRIORITY if cat in by_category]
    for cat in sorted(by_category):
        if cat not in ordered_categories:
            ordered_categories.append(cat)

    if not diversify:
        ordered_categories = [""]
        by_category[""] = sorted(unique, key=lambda x: (x.category, x.language, x.name.casefold(), x.url))

    interleaved: list[Channel] = []
    per_source: Counter[str] = Counter()
    while True:
        added_in_round = False
        for cat in ordered_categories:
            bucket = by_category[cat]
            while bucket:
                candidate = bucket.pop(0)
                if max_per_source > 0 and per_source[_source_bucket(candidate)] >= max_per_source:
                    continue
                interleaved.append(candidate)
                per_source[_source_bucket(candidate)] += 1
                added_in_round = True
                break
        if not added_in_round:
            break
        if limit and limit > 0 and len(interleaved) >= limit:
            return interleaved

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
    deadline: float | None = None,
    max_per_source: int = 0,
    require_playlist: bool = False,
    resolve_variants: bool = True,
) -> tuple[list[Channel], dict[str, StreamCheck]]:
    """Probe candidate streams in bounded batches until ``limit`` unique channels are found.

    This avoids checking 10,000+ URLs when only 20 working, non-repeating channels are needed.
    ``deadline`` is a ``time.monotonic()`` value; once it passes no further batches are probed.
    """
    candidates = _dedupe(channels)
    if not candidates:
        return [], {}

    if limit <= 0:
        checks = check_streams(candidates, timeout=timeout, workers=workers,
                               deadline=deadline, resolve_variants=resolve_variants)
        selected = dedupe_unique_channels(
            candidates,
            limit=None,
            stream_checks=checks,
            only_http_ok=only_http_ok,
            exclude_keys=exclude_keys,
            max_per_source=max_per_source,
            require_playlist=require_playlist,
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

    # Respect the caller's cap exactly, even when it is smaller than ``limit``.
    # In that case the selector may return fewer channels rather than probe extra URLs.
    probe_cap = max_probes if max_probes > 0 else len(probe_queue)
    probe_queue = probe_queue[:probe_cap]

    all_checks: dict[str, StreamCheck] = {}
    probed_channels: list[Channel] = []
    batch_size = max(limit, min(max(limit * 2, 24), 48))

    for offset in range(0, len(probe_queue), batch_size):
        remaining = _remaining_seconds(deadline)
        if remaining is not None and remaining <= 1:
            log(
                "WARNING: se agotó el tiempo de comprobación; se exporta lo verificado "
                f"hasta ahora ({len(all_checks)} URLs comprobadas).",
                level="warning", file=sys.stderr,
            )
            break
        batch = probe_queue[offset:offset + batch_size]
        batch_checks = check_streams(batch, timeout=timeout, workers=workers,
                                     deadline=deadline, resolve_variants=resolve_variants)
        all_checks.update(batch_checks)
        probed_channels.extend(batch)
        log(
            f"  · streams: {len(all_checks)}/{len(probe_queue)} URLs comprobadas "
            f"({sum(1 for chk in batch_checks.values() if chk.status == 'http_ok')} ok "
            f"en el lote de {len(batch_checks)})",
            "debug",
        )

        ok_unique = dedupe_unique_channels(
            probed_channels,
            limit=limit,
            stream_checks=all_checks,
            only_http_ok=True,
            exclude_keys=exclude_keys,
            max_per_source=max_per_source,
            require_playlist=require_playlist,
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
        max_per_source=max_per_source,
        require_playlist=require_playlist,
    )
    return selected, all_checks


STATIC_ASSET_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|avif|svg|ico|css|js|mjs|json|woff2?|ttf|eot|mp4|mp3|webm|zip|pdf|txt)$",
    re.I,
)
def _looks_like_opaque_token(segment: str) -> bool:
    """Un segmento de ruta sin separadores y con forma de hash/base64/uuid.

    Los slugs legibles (`tyc-sports`, `c5n`) y los ids numéricos (`/ver/202608150001`)
    se conservan siempre; lo que se descarta son cadenas largas sin separadores que
    mezclan mayúsculas, minúsculas y dígitos (o terminan en `=`), típico de un token,
    un hash o un base64 metido en un `data-*`.
    """
    if "-" in segment or "_" in segment or "." in segment:
        return False
    if segment.isdigit():
        return False  # un id numérico largo sigue siendo una página válida
    if segment.endswith("="):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{16,}", segment) and re.search(r"[a-fA-F]", segment):
        return True
    if len(segment) < 24 or not re.fullmatch(r"[A-Za-z0-9+/]{24,}", segment):
        return False
    return bool(re.search(r"[A-Z]", segment) and re.search(r"[a-z]", segment)
                and re.search(r"\d", segment))


def _looks_like_channel_page(url: str) -> bool:
    """Descarta lo que no puede ser una página de canal al descubrir páginas.

    Evita que un `data-src` con el stream en base64, un hash o una imagen acaben
    descargándose como si fueran la página de un canal (peticiones perdidas y ruido
    en los avisos).
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not last:
        return False
    if STATIC_ASSET_RE.search(last):
        return False
    if _looks_like_opaque_token(last):
        return False
    return True


def _page_matches_source(source: SourceConfig, path: str) -> bool:
    """Decide si una ruta del sitio es una página de canal según la configuración."""
    if not source.page_prefixes and not source.page_patterns:
        return True
    lowered = path.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in source.page_prefixes):
        return True
    return any(re.search(pattern, path, re.I) for pattern in source.page_patterns)


def _sitemap_locations(document: str, base_url: str) -> list[str]:
    """Extract absolute URLs from a standard XML sitemap or sitemap index."""
    locations = re.findall(r"<loc\b[^>]*>\s*(.*?)\s*</loc\s*>", document, re.I | re.S)
    urls = []
    for location in locations:
        value = unescape(location.strip())
        if value.startswith("<![CDATA[") and value.endswith("]]>"):
            value = value[9:-3].strip()
        url = _clean_url(value, base_url)
        if urlparse(url).scheme.lower() in {"http", "https"} and url not in urls:
            urls.append(url)
    return urls


def _fetch_sitemap_pages(source: SourceConfig, timeout: int,
                         max_pages: int) -> tuple[list[str], list[str]]:
    """Read a bounded chain of sitemaps and return matching pages."""
    if max_pages <= 0 or not source.sitemap_urls:
        return [], []
    pending = list(source.sitemap_urls)
    seen_sitemaps: set[str] = set()
    pages: list[str] = []
    errors: list[str] = []
    sitemap_count = 0
    while pending and sitemap_count < 12:
        sitemap_url = pending.pop(0)
        sitemap_key = _url_key(sitemap_url)
        if not sitemap_key or sitemap_key in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_key)
        try:
            document = _fetch_text(sitemap_url, timeout, max_bytes=DEFAULT_MAX_BYTES)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.key}: sitemap {sitemap_url}: {exc}")
            sitemap_count += 1
            continue
        sitemap_count += 1
        locations = _sitemap_locations(document, sitemap_url)
        child_sitemaps = [
            url for url in locations
            if urlparse(url).path.lower().endswith((".xml", ".xml.gz"))
        ]
        if child_sitemaps:
            pending.extend(child_sitemaps[:12 - sitemap_count])
            continue
        source_host = urlparse(source.url).netloc.lower()
        for url in locations:
            parsed = urlparse(url)
            if parsed.netloc.lower() != source_host:
                continue
            if not _page_matches_source(source, parsed.path):
                continue
            pages.append(url)
    return pages, errors


def _select_site_pages(source: SourceConfig, pages: list[tuple[str, str]],
                       max_pages: int) -> list[tuple[str, str]]:
    """Limit site pages, sampling one country at a time for country-coded sites."""
    if max_pages <= 0:
        return []
    unique: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for page, label in pages:
        key = _url_key(page)
        if not key:
            continue
        if key in seen:
            index = seen[key]
            if not unique[index][1] and label:
                unique[index] = (page, label)
            continue
        seen[key] = len(unique)
        unique.append((page, label))
    if not source.country_path_prefix:
        return unique[:max_pages]

    buckets: dict[str, list[tuple[str, str]]] = {}
    for index, item in enumerate(unique):
        country = _country_from_source_path(source, item[0])
        bucket = country or f"__page_{index}"
        buckets.setdefault(bucket, []).append(item)
    selected: list[tuple[str, str]] = []
    round_index = 0
    while len(selected) < max_pages:
        added = False
        for bucket in buckets.values():
            if round_index < len(bucket):
                selected.append(bucket[round_index])
                added = True
                if len(selected) >= max_pages:
                    break
        if not added:
            break
        round_index += 1
    return selected


def _fetch_site_page(source: SourceConfig, page: str, label: str,
                     timeout: int) -> tuple[list[Channel], str | None]:
    """Descarga una página de canal y resuelve su reproductor.

    Partiendo de la página del canal se siguen iframes, wrappers PHP (`/html/fl/`),
    configuraciones JSON (`cv.json` → `cvatt.html`) y embeds de YouTube en vivo hasta
    ``source.max_depth`` saltos, siempre con un número acotado de documentos.
    """
    fetch_headers = _source_headers(source)
    try:
        # `use_cache`: muchas páginas enlazan el mismo wrapper o la misma lista; repetir
        # la descarga del mismo documento solo cuesta tiempo y peticiones de más.
        root_html = _fetch_text(page, timeout, headers=fetch_headers or None, use_cache=True)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return [], f"{source.key}: page {page}: {exc}"

    raw_name = clean_channel_name(label) if label else ""
    if not raw_name or _canonical_channel_key(raw_name) in GENERIC_CHANNEL_NAMES:
        raw_name = _page_title(root_html, source.name, clean=not bool(source.title_pattern))
        if source.title_pattern:
            match = re.search(source.title_pattern, raw_name, re.I | re.S)
            if match and match.group(1).strip():
                raw_name = match.group(1).strip()
    name = clean_channel_name(raw_name) or raw_name or source.name
    logo = _page_logo(root_html, page)

    found: list[Channel] = []
    errors: list[str] = []
    for stream in _stream_urls_from_document(root_html, page):
        found.append(_site_channel(name, stream, source, page, logo))
    for embed in _youtube_embed_urls(root_html):
        found.append(_site_channel(name, embed, source, page, logo, STREAM_TYPE_YOUTUBE))

    allowed_hosts = {urlparse(source.url).netloc.lower()}
    allowed_hosts.update(host.lower() for host in source.embed_hosts)
    queue: list[tuple[str, int]] = []
    seen: set[str] = {_url_key(page)}
    documents = 1

    def enqueue(candidates: Iterable[str], depth: int) -> None:
        for candidate in candidates:
            key = _url_key(candidate)
            if not key or key in seen:
                continue
            if len(queue) >= MAX_PLAYER_CANDIDATES * 2:
                return
            seen.add(key)
            queue.append((candidate, depth))

    def process_json_configs(document: str, document_url: str, depth: int) -> None:
        for config_url in _json_config_urls(document, document_url):
            try:
                payload = json.loads(_fetch_text(
                    config_url, timeout, max_bytes=4 * 1024 * 1024,
                    headers=fetch_headers, use_cache=True,
                ))
            except (HTTPError, URLError, TimeoutError, OSError, ValueError,
                    json.JSONDecodeError) as exc:
                errors.append(f"{source.key}: config {config_url}: {exc}")
                continue
            json_channels = _channels_from_json_payload(
                payload, source, config_url, source_page=page,
            )
            found.extend(json_channels)
            config_urls = _player_urls_from_config(payload, document, document_url)
            allowed_hosts.update(urlparse(candidate).netloc.lower() for candidate in config_urls)
            if not json_channels:
                for stream_url in (candidate for candidate in config_urls if _is_stream_url(candidate)):
                    found.append(_site_channel(name, stream_url, source, page, logo))
            player_urls = [candidate for candidate in config_urls if not _is_stream_url(candidate)]
            # Solo se prueban los primeros candidatos: suelen ser espejos del mismo player.
            if depth <= source.max_depth:
                enqueue(player_urls[:2], depth)

    if source.max_depth > 0:
        iframes, js_urls = _player_links_from_document(root_html, page)
        scripts = _script_urls_from_document(root_html, page, allowed_hosts)
        enqueue(_without_youtube(iframes), 1)
        enqueue([url for url in js_urls
                 if urlparse(url).netloc.lower() in allowed_hosts], 1)
        enqueue(scripts, 1)
        process_json_configs(root_html, page, 1)

    while queue and documents < MAX_PLAYER_DOCS:
        url, depth = queue.pop(0)
        try:
            html = _fetch_text(url, timeout, headers=fetch_headers or None, use_cache=True)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.key}: player {url}: {exc}")
            continue
        documents += 1
        log(f"    · {source.key}: sigue el player {url} (doc {documents})", "debug")

        if _looks_like_m3u(html):
            entries = parse_m3u(html, source=source.name, base_url=url)
            for channel in entries:
                channel.name = name or channel.name
                channel.group = channel.group or source.name
                channel.source_url = page
                _apply_source_country(channel, source, page)
                found.append(channel)
            if not entries:
                # El wrapper devolvió una playlist HLS de un solo canal (master o de
                # segmentos): la propia URL descargada es el stream.
                playlist = inspect_hls_playlist(html, url)
                stream_url = url
                if playlist.kind == "master" and playlist.variants:
                    stream_url = playlist.variants[0].url
                if playlist.kind in {"master", "media"}:
                    found.append(_site_channel(name, stream_url, source, page, logo))
            continue

        if not logo:
            logo = _page_logo(html, url)
        for stream in _stream_urls_from_document(html, url):
            found.append(_site_channel(name, stream, source, page, logo))
        for embed in _youtube_embed_urls(html):
            found.append(_site_channel(name, embed, source, page, logo, STREAM_TYPE_YOUTUBE))

        # Some sites publish channel data in a linked/embedded JSON document rather
        # than placing the stream URL directly in the page or player wrapper.
        process_json_configs(html, url, depth + 1)
        if depth >= source.max_depth:
            continue
        iframes, js_urls = _player_links_from_document(html, url)
        scripts = _script_urls_from_document(html, url, allowed_hosts)
        enqueue(_without_youtube(iframes), depth + 1)
        enqueue([candidate for candidate in js_urls
                 if urlparse(candidate).netloc.lower() in allowed_hosts], depth + 1)
        enqueue(scripts, depth + 1)

    return _dedupe(found), ("; ".join(errors[:3]) if errors and not found else None)


def _expand_nested_playlists(source: SourceConfig, channels: list[Channel], timeout: int,
                             limit: int, fetch_headers: dict[str, str] | None = None,
                             deadline: float | None = None,
                             ) -> tuple[list[Channel], list[str]]:
    """Despliega las sub-listas `.m3u` enlazadas por una lista maestra.

    Varias fuentes públicas (`m3u.cl`, iptv-org, listas de canales por país) publican
    un índice cuyas entradas apuntan a otros `.m3u`. Sin este paso el índice aporta
    “canales” que ningún reproductor puede abrir; con él, cada sub-lista se descarga
    una vez (paralelo y acotado) y sus canales reales entran en el resultado.
    """
    nested = [
        channel for channel in channels
        if urlparse(channel.url).path.lower().endswith(".m3u")
    ]
    if not nested or limit <= 0:
        return channels, []
    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining < NESTED_MIN_SECONDS:
        # Desplegar sub-listas cuesta peticiones extra; sin presupuesto se deja la
        # lista tal cual (las entradas-índice se descartan al comprobar streams).
        return channels, [
            f"{source.key}: sub-listas no desplegadas por falta de tiempo "
            f"({len(nested)} pendientes)",
        ]
    if remaining is not None:
        timeout = max(1, min(timeout, int(remaining / 2) or 1))
    errors: list[str] = []
    expanded: dict[str, list[Channel]] = {}
    queue = nested[:limit]
    workers = max(1, min(6, len(queue)))

    def collect(channel: Channel, content: str) -> list[Channel]:
        items = parse_m3u(content, source=source.name, base_url=channel.url)[:MAX_NESTED_ENTRIES]
        for item in items:
            item.group = item.group or channel.name
            item.source_url = channel.url
            item.headers = item.headers or channel.headers
            item.directives = item.directives or channel.directives
            _apply_source_country(item, source, channel.url)
        return items

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(_fetch_text, channel.url, timeout, DEFAULT_MAX_BYTES,
                        2, fetch_headers or None, True): channel
            for channel in queue
        }
        iterator = as_completed(futures, timeout=_remaining_seconds(deadline)) \
            if deadline is not None else as_completed(futures)
        try:
            for future in iterator:
                channel = futures[future]
                try:
                    content = future.result()
                except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                    errors.append(f"{source.key}: sub-lista {channel.url}: {str(exc)[:160]}")
                    continue
                if not _looks_like_m3u(content):
                    errors.append(f"{source.key}: sub-lista {channel.url}: no es una lista M3U")
                    continue
                items = collect(channel, content)
                if items:
                    expanded[channel.url] = items
        except FuturesTimeout:
            errors.append(f"{source.key}: sub-listas incompletas (se acabó el tiempo)")
    finally:
        # No esperar hebras colgadas: el presupuesto de la ejecución manda.
        pool.shutdown(wait=False, cancel_futures=True)

    log(f"  · {source.key}: {len(expanded)} sub-listas desplegadas", "debug")
    if not expanded:
        return channels, errors
    # Se sustituyen las entradas-índice ya desplegadas y se conservan las que fallaron.
    result = [
        channel for channel in channels
        if channel.url not in expanded
    ]
    for channel in channels:
        items = expanded.get(channel.url)
        if items:
            result.extend(items)
    return result, errors


def _root_document_channels(source: SourceConfig, html: str, document_url: str, *,
                            strict: bool) -> list[Channel]:
    """Canales publicados directamente en el documento de entrada de un sitio."""
    streams = _stream_urls_from_document(html, document_url)
    if strict:
        streams = [
            url for url in streams
            if urlparse(url).path.lower().endswith((".m3u8", ".mpd"))
        ]
    embeds = [] if streams else _youtube_embed_urls(html)
    if not streams and not embeds:
        return []
    name = clean_channel_name(_page_title(html, source.name)) or source.name
    logo = _page_logo(html, document_url)
    found = [_site_channel(name, url, source, document_url, logo) for url in streams]
    found += [
        _site_channel(name, url, source, document_url, logo, STREAM_TYPE_YOUTUBE)
        for url in embeds[:1]
    ]
    return _dedupe(found)


def _fetch_source(source: SourceConfig, timeout: int, max_pages: int,
                  max_nested: int = DEFAULT_MAX_NESTED_PLAYLISTS,
                  deadline: float | None = None,
                  ) -> tuple[list[Channel], list[str]]:
    errors: list[str] = []
    attempt_errors: list[str] = []
    initial = ""
    effective_url = ""
    fetch_headers = _source_headers(source)
    kwargs: dict[str, object] = {"headers": fetch_headers} if fetch_headers else {}
    candidate_urls = list(dict.fromkeys((source.url, *source.fallback_urls)))
    for candidate_url in candidate_urls:
        try:
            document = _fetch_text(candidate_url, timeout, **kwargs)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            attempt_errors.append(f"{candidate_url}: {exc}")
            continue
        if source.kind == "playlist" and not _looks_like_m3u(document):
            attempt_errors.append(f"{candidate_url}: response is not an M3U playlist")
            continue
        initial = document
        effective_url = candidate_url
        break
    if not effective_url:
        if not attempt_errors:
            attempt_errors.append("no source URL configured")
        return [], [f"{source.key}: {message}" for message in attempt_errors]

    if source.kind == "playlist" or _looks_like_m3u(initial):
        channels = parse_m3u(initial, source=source.name, base_url=effective_url)
        if not channels:
            # El archivo enlazado es la playlist HLS de un solo canal (variantes o
            # segmentos): no hay lista de emisoras, pero sí un stream aprovechable.
            playlist = inspect_hls_playlist(initial, effective_url)
            single = ""
            if playlist.kind == "master" and playlist.variants:
                single = playlist.variants[0].url
            elif playlist.kind == "media":
                single = effective_url
            if single:
                raw_name = _name_from_url(effective_url) or source.name
                name = clean_channel_name(raw_name) or raw_name
                category, language, country = classify_channel(name, source.name)
                channels = [Channel(
                    name=name, url=single, group=source.name, logo="",
                    language=language, country=country, category=category,
                    source=source.name, source_url=effective_url,
                )]
        for channel in channels:
            channel.source_url = effective_url
            _apply_source_country(channel, source, effective_url)
        if source.expand_nested:
            channels, nested_errors = _expand_nested_playlists(
                source, channels, timeout, max_nested, fetch_headers or None, deadline,
            )
            errors.extend(nested_errors)
        return channels, errors

    parser = LinkParser()
    parser.feed(initial)
    parser.close()
    playlists = list(source.playlist_hints)
    playlists += [u for u in _urls_from_html(initial, effective_url) if ".m3u" in u.lower()]

    channels: list[Channel] = []
    seen_playlists: set[str] = set()
    for playlist in playlists:
        if playlist in seen_playlists:
            continue
        seen_playlists.add(playlist)
        try:
            # Con caché: si una lista se enlaza varias veces no se descarga dos veces. Que
            # la misma URL vuelva a pedirse como wrapper de un canal NO es un desperdicio:
            # ahí `fetch_headers` lleva el Referer de la página del canal, y un CDN con
            # anti-hotlinking puede responder 403/404 con el Referer de la raíz y 200 con
            # el correcto (la caché está indexada por URL+cabeceras, por eso no colisiona).
            content = _fetch_text(playlist, timeout, headers=fetch_headers or None,
                                  use_cache=True)
            if _looks_like_m3u(content):
                items = parse_m3u(content, source=source.name, base_url=playlist)
                for channel in items:
                    channel.source_url = playlist
                    _apply_source_country(channel, source, playlist)
                channels.extend(items)
            else:
                errors.append(f"{source.key}: playlist {playlist}: response is not an M3U playlist")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.key}: playlist {playlist}: {exc}")

    # El documento de entrada también puede traer el reproductor: ocurre al apuntar
    # `--source-url` a la página de un canal concreto. Desde la raíz del sitio solo se
    # aceptan streams con extensión de playlist, para no tomar enlaces `/live/xxx` del
    # menú como si fueran canales.
    entry_is_root = urlparse(effective_url).path in ("", "/")
    root_channels = _root_document_channels(source, initial, effective_url,
                                            strict=entry_is_root)

    host = urlparse(effective_url).netloc.lower()
    pages: list[tuple[str, str]] = []
    seen_pages: dict[str, int] = {}

    def add_page(page: str, label: str = "") -> None:
        parsed = urlparse(page)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host:
            return
        if not _page_matches_source(source, parsed.path):
            return
        if not _looks_like_channel_page(page):
            return
        key = _url_key(page)
        if key in seen_pages:
            index = seen_pages[key]
            if not pages[index][1] and label:
                pages[index] = (page, label)
            return
        seen_pages[key] = len(pages)
        pages.append((page, label))

    sitemap_pages, sitemap_errors = _fetch_sitemap_pages(source, timeout, max_pages)
    errors.extend(sitemap_errors)
    for page in sitemap_pages:
        add_page(page)
    for href, label in parser.links:
        add_page(_clean_url(href, effective_url), label)

    pages = _select_site_pages(source, pages, max_pages)
    log(f"  · {source.key}: {len(pages)} páginas de canal para rastrear", "debug")
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

        for page_no, (page_channels, page_error) in enumerate(page_results):
            channels.extend(page_channels)
            log(
                f"    · {source.key}: {len(page_channels)} streams en la página "
                f"{page_no + 1}/{len(page_results)}",
                "debug",
            )
            if page_error:
                errors.append(page_error)
                log(f"    · {source.key}: {page_error}", "debug")

    return _dedupe(channels + root_channels), errors


def fetch_sources(
    sources: Iterable[SourceConfig], timeout: int = 20,
    workers: int = 4, max_pages: int = 30,
    deadline: float | None = None,
    max_nested: int = DEFAULT_MAX_NESTED_PLAYLISTS,
) -> tuple[list[Channel], dict[str, list[str]], dict[str, int]]:
    """Fetch every source in parallel and return channels, errors and per-source counts."""
    source_list = list(sources)
    if not source_list:
        return [], {}, {}

    results: list[tuple[list[Channel], list[str]]] = [([], []) for _ in source_list]
    worker_count = max(1, min(workers, len(source_list)))
    pool = ThreadPoolExecutor(max_workers=worker_count)
    try:
        futures = {
            pool.submit(_fetch_source, source, timeout, max_pages, max_nested,
                        deadline): (index, source)
            for index, source in enumerate(source_list)
        }
        waiter = as_completed(futures, timeout=_remaining_seconds(deadline)) \
            if deadline is not None else as_completed(futures)
        try:
            for future in waiter:
                index, source = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = ([], [f"{source.key}: unexpected error: {exc}"])
        except FuturesTimeout:
            for future, (index, source) in futures.items():
                if not future.done():
                    future.cancel()
                    results[index] = ([], [f"{source.key}: sin tiempo de respuesta"])
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    channels: list[Channel] = []
    errors: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for source, (items, source_errors) in zip(source_list, results):
        channels.extend(items)
        counts[source.key] = len(items)
        if source_errors:
            errors[source.key] = source_errors
    channels = _dedupe(channels)
    channels.sort(key=lambda x: (x.category, x.language, x.name.casefold(), x.url))
    return channels, errors, counts


def _headers_to_m3u(headers: dict[str, str] | None) -> list[str]:
    """Directivas `#EXTVLCOPT`/`#EXTHTTP` para que la lista exportada se reproduzca.

    Las tres cabeceras que entienden todos los reproductores van como `#EXTVLCOPT`;
    el resto se agrupa en un unico `#EXTHTTP` JSON, que es lo que se vuelve a leer al
    reimportar la lista (round-trip sin perder nada).
    """
    lines: list[str] = []
    extra: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if not key or not value:
            continue
        name = key.strip().casefold()
        if name == "user-agent":
            lines.append(f"#EXTVLCOPT:http-user-agent={value}")
        elif name in {"referer", "referrer"}:
            lines.append(f"#EXTVLCOPT:http-referrer={value}")
        elif name == "cookie":
            extra["cookie"] = value
        else:
            extra[key.strip()] = value
    if extra:
        lines.append("#EXTHTTP:" + json.dumps(extra, ensure_ascii=False))
    return lines


def _headers_to_text(headers: dict[str, str] | None) -> str:
    return "; ".join(f"{key}: {value}" for key, value in (headers or {}).items() if value)


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
            if channel.stream_type and channel.stream_type != STREAM_TYPE_HLS:
                attrs.append(f'stream-type="{channel.stream_type}"')
            # `other` no es un grupo útil en el reproductor: solo se usa si viene dado.
            group_title = channel.group or (
                channel.category if channel.category not in {"", "other"} else ""
            )
            if group_title:
                attrs.append(f'group-title="{group_title}"')
            attr_str = (" " + " ".join(attrs)) if attrs else ""
            lines.append(f"#EXTINF:-1{attr_str},{channel.name}")
            lines.extend(_headers_to_m3u(channel.headers))
            for directive in channel.directives or []:
                # Se reescribe tal cual: #EXT-X-KEY/#KODIPROP/EXTVLCOPT sueltos que no
                # son cabeceras y que el reproductor necesita leer antes de la URL.
                if directive and directive.startswith("#"):
                    lines.append(directive)
            lines.append(channel.url)
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    fields = ["name", "url", "group", "tvg_id", "tvg_name", "logo",
              "language", "country", "category", "source", "source_url", "stream_type",
              "headers"]
    csv_rows = []
    for channel, row in zip(channel_list, rows):
        csv_row = dict(row)
        csv_row["headers"] = _headers_to_text(channel.headers)
        csv_rows.append(csv_row)
    if stream_checks is not None:
        fields.extend(("stream_status", "stream_http_status", "stream_content_type",
                       "stream_final_url", "stream_detail", "stream_playlist_kind",
                       "stream_media_url", "stream_resolution", "stream_segment_count"))
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
                "stream_playlist_kind": check.playlist_kind,
                "stream_media_url": check.media_url,
                "stream_resolution": check.resolution,
                "stream_segment_count": check.segment_count,
                "headers": _headers_to_text(channel.headers),
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
    payload: object
    try:
        _apply_socket_timeout(timeout)
        with urlopen(request, timeout=max(1, timeout)) as response:
            raw = response.read(10 * 1024 * 1024)
            payload = json.loads(_decode_body(
                raw, response.headers.get("Content-Encoding", "")
            ).decode("utf-8", errors="replace"))
    except Exception as exc:
        # Sin la lista previa no se puede evitar repetir; se avisa y se continúa.
        log(
            f"WARNING: no se pudieron leer los canales existentes de Supabase: {exc}",
            "warning", file=sys.stderr,
        )
        return set(), set(), set()

    if not isinstance(payload, list):
        log("WARNING: Supabase devolvió una respuesta inesperada al listar canales.",
            "warning", file=sys.stderr)
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
            "stream_type": channel.stream_type or STREAM_TYPE_HLS,
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


def _write_step_summary(channels: list[Channel], source_counts: dict[str, int],
                        errors: dict[str, list[str]], synced: int | None,
                        sync_error: str) -> None:
    """Write a markdown summary so GitHub Actions shows exactly what was extracted."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["## SDF — extracción de canales", ""]
    lines.append(f"- Canales exportados: **{len(channels)}**")
    if synced is not None:
        lines.append(f"- Canales sincronizados en Supabase: **{synced}**")
    elif sync_error:
        lines.append(f"- Sincronización con Supabase: **falló** (`{sync_error}`)")
    lines.append("")
    if source_counts:
        lines.append("| Fuente | Canales | Avisos |")
        lines.append("| --- | --- | --- |")
        for key, count in sorted(source_counts.items()):
            lines.append(f"| `{key}` | {count} | {len(errors.get(key, ()))} |")
        lines.append("")
    if errors:
        lines.append("<details><summary>Avisos por fuente</summary>")
        lines.append("")
        for key, source_errors in sorted(errors.items()):
            for error in source_errors[:5]:
                lines.append(f"- `{key}`: {error}")
        lines.append("")
        lines.append("</details>")
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


# TLD que se usan como "domain hack" comercial y no como señal de país.
AMBIGUOUS_TLDS = {
    "tv", "me", "fm", "am", "ly", "io", "gg", "to", "cc", "ai", "im", "sh", "li", "st",
    "be", "at", "ch", "de",
}
GENERIC_SECOND_LEVEL = {"com", "org", "net", "gov", "edu", "mil", "ac"}


def _country_from_host(netloc: str) -> str:
    """País sugerido por el ccTLD del dominio (``m3u.cl`` → ``CL``).

    Solo se usa como pista (rellena países desconocidos), igual que ``country_hint``.
    """
    host = (netloc or "").split(":", 1)[0].lower().rstrip(".")
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return ""
    tld = labels[-1]
    if len(tld) != 2 or tld in AMBIGUOUS_TLDS:
        return ""
    if len(labels) >= 3 and labels[-2] in GENERIC_SECOND_LEVEL:
        # `www.lista.com.ar`: el país lo da el último segmento igualmente.
        pass
    return _code(tld, COUNTRY_CODES, upper=True)


def _custom_sources(urls: Iterable[str], name: str = "") -> tuple[SourceConfig, ...]:
    """Convierte URLs sueltas de la CLI en fuentes configuradas.

    ``.m3u``/``.m3u8`` se tratan como lista directa; el resto se rastrea como sitio con
    reproductor, igual que las fuentes ``kind="site"`` incluidas en el proyecto.
    """
    out: list[SourceConfig] = []
    for index, raw in enumerate(urls, start=1):
        url = (raw or "").strip()
        if not url:
            continue
        if "://" not in url:
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            log(f"WARNING: --source-url ignorada (no es HTTP/HTTPS): {raw}",
                level="warning", file=sys.stderr)
            continue
        if "." not in parsed.netloc.split(":", 1)[0]:
            log(f"WARNING: --source-url ignorada (dominio sin sufijo): {raw}",
                level="warning", file=sys.stderr)
            continue
        host = parsed.netloc.lower()
        label = (name or host).strip()
        key = f"custom{'' if index == 1 else index}-{host.split(':')[0].replace('.', '-')}"
        country_hint = _country_from_host(parsed.netloc)
        is_playlist = parsed.path.lower().endswith((".m3u", ".m3u8"))
        if is_playlist:
            out.append(SourceConfig(key=key, name=label, url=url, kind="playlist",
                                    country_hint=country_hint))
            continue
        out.append(SourceConfig(
            key=key, name=label, url=url, kind="site",
            # Sin filtros de ruta: se explora el sitio hasta `--max-pages`.
            max_depth=2, country_hint=country_hint,
        ))
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    # `global` en la primera línea: POLITE_DELAY se lee más abajo y Python lo exige así.
    global POLITE_DELAY
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
        "--search", type=str, default="",
        help="Buscar canales por texto (insensible a mayúsculas/acentos). "
             "Acepta comas como OR y espacios como AND dentro de cada alternativa. "
             "Ej: --search \"espn colombia\"  o  --search \"cnn, bbc\"",
    )
    parser.add_argument(
        "--search-fields", type=str, default=",".join(DEFAULT_SEARCH_FIELDS),
        help="Campos donde buscar con --search (separados por comas): "
             + ", ".join(sorted(VALID_SEARCH_FIELDS))
             + f" (por defecto: {','.join(DEFAULT_SEARCH_FIELDS)})",
    )
    parser.add_argument(
        "--regex", "--search-regex", dest="search_regex", action="store_true",
        help="Interpretar --search como expresión regular (insensible a mayúsculas/acentos)",
    )
    parser.add_argument(
        "--limit", "--max-channels", dest="limit", type=int, default=DEFAULT_MAX_CHANNELS,
        help=f"Maximum number of unique non-repeating channels to export/sync (default: {DEFAULT_MAX_CHANNELS}; 0 for unlimited)",
    )
    parser.add_argument(
        "--max-checks", type=int, default=DEFAULT_MAX_STREAM_PROBES,
        help=f"Maximum candidate URLs to probe when --check-streams and --limit are used (default: {DEFAULT_MAX_STREAM_PROBES})",
    )
    parser.add_argument(
        "--max-per-source", type=int, default=0,
        help="Máximo de canales por fuente al repartir el cupo (0 = sin límite, "
             "recomendado al usar --all-sources para que ninguna lista acapare el resultado)",
    )
    parser.add_argument("--source-url", action="append", default=[], metavar="URL",
                        help="Fuente puntual adicional (lista .m3u/.m3u8 o sitio con "
                             "reproductor); se puede repetir. Se infiere el tipo por extensión")
    parser.add_argument("--source-name", default="", metavar="NOMBRE",
                        help="Etiqueta para las fuentes indicadas con --source-url")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--log-level",
                        default=os.environ.get("SDF_LOG_LEVEL", "INFO"),
                        help="detalle de la salida: DEBUG, INFO, WARNING o ERROR "
                             "(DEBUG añade el progreso del rastreo y de cada comprobación)")
    parser.add_argument("--polite-delay", type=float, default=POLITE_DELAY,
                        help="Intervalo mínimo en segundos entre peticiones al mismo host "
                             f"(por defecto: {POLITE_DELAY:g})")
    parser.add_argument("--max-nested-playlists", type=int, default=DEFAULT_MAX_NESTED_PLAYLISTS,
                        help="Sub-listas .m3u a desplegar por fuente (0 desactiva la "
                             f"expansión; por defecto: {DEFAULT_MAX_NESTED_PLAYLISTS})")
    parser.add_argument("--require-playlist", action="store_true",
                        help="Conservar solo los canales cuyo .m3u8 responde una playlist "
                             "HLS real (master o de segmentos); descarta páginas HTML")
    parser.add_argument("--no-resolve-variants", dest="resolve_variants", action="store_false",
                        help="No resolver los master playlist HLS a su variante jugable "
                             "(más rápido, pero `media_url` queda vacío)")
    parser.add_argument("--deadline", type=int, default=0,
                        help="Tiempo máximo en segundos para descargar fuentes y comprobar "
                             "streams (0 = sin límite)")
    parser.add_argument("--check-streams", action="store_true",
                        help="Probe a small HTTP byte range and include reachability results")
    parser.add_argument("--sync-supabase", action="store_true",
                        help="Upsert the result into public.tv_channels")
    parser.add_argument("--fail-on-sync-error", action="store_true",
                        help="Return a non-zero exit code when the Supabase sync fails")
    parser.add_argument("--activation-mode", choices=("manual", "automatic"), default="manual",
                        help="manual activates imported channels; automatic activates only HTTP-ok streams")
    parser.add_argument("--supabase-country", default="US",
                        help="Country fallback when the channel country is unknown (must exist in countries)")
    args = parser.parse_args(argv)

    # El nivel de log se valida aquí (los workflows de GitHub pasan --log-level).
    try:
        configure_logging(args.log_level)
    except ValueError as exc:
        parser.error(str(exc))

    if args.list_sources:
        for source in DEFAULT_SOURCES:
            urls = " | ".join((source.url, *source.fallback_urls))
            print(f"{source.key}\t{source.name}\t{urls}")
        return 0

    POLITE_DELAY = max(0.0, args.polite_delay)
    clear_fetch_cache()
    if not args.list_sources:
        _apply_socket_timeout(max(1, args.timeout))

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
            # Antes esto abortaba con exit code 2 y no se guardaba ningún canal.
            log(
                "WARNING: --sync-supabase ignorado: faltan SUPABASE_URL y "
                "SUPABASE_SERVICE_ROLE_KEY (o SUPABASE_SECRET_KEY). "
                "Se exporta el archivo sin sincronizar.",
                level="warning", file=sys.stderr,
            )
            args.sync_supabase = False
        else:
            existing_keys = fetch_existing_supabase_channels(
                supabase_url, supabase_key, timeout=max(1, args.timeout),
            )

    if args.activation_mode == "automatic" and not args.check_streams:
        log(
            "WARNING: --activation-mode automatic necesita --check-streams; "
            "se activa la comprobación de streams.",
            level="warning", file=sys.stderr,
        )
        args.check_streams = True

    channels: list[Channel] = []
    for path in args.input:
        if not path.is_file():
            parser.error(f"input file does not exist: {path}")
        channels.extend(read_playlist(path))

    extra_sources = _custom_sources(args.source_url, args.source_name)
    if args.all_sources:
        selected = tuple(DEFAULT_SOURCES) + extra_sources
    elif args.source or extra_sources:
        keys = set(args.source or [])
        selected = tuple(
            source for source in DEFAULT_SOURCES if source.key in keys
        ) + extra_sources
    else:
        selected = ()

    deadline = time.monotonic() + args.deadline if args.deadline and args.deadline > 0 else None

    if selected:
        remote_channels, errors, source_counts = fetch_sources(
            selected, timeout=max(1, args.timeout),
            workers=max(1, args.workers), max_pages=max(0, args.max_pages),
            deadline=deadline, max_nested=max(0, args.max_nested_playlists),
        )
        channels.extend(remote_channels)
    else:
        errors = {}
        source_counts = {}

    channels = _dedupe(channels)
    if args.category:
        channels = [c for c in channels if c.category == args.category.casefold()]
    if args.language:
        channels = [c for c in channels if c.language == args.language.casefold()]
    if args.country:
        channels = [c for c in channels if c.country == args.country.upper()]
    if args.search:
        search_fields = [f.strip() for f in args.search_fields.split(",") if f.strip()]
        invalid = [f for f in search_fields if f.casefold() not in VALID_SEARCH_FIELDS]
        if invalid:
            parser.error(
                f"--search-fields contiene campos no válidos: {', '.join(invalid)} "
                f"(válidos: {', '.join(sorted(VALID_SEARCH_FIELDS))})"
            )
        pre_count = len(channels)
        channels = search_channels(
            channels, args.search, fields=search_fields, use_regex=args.search_regex,
        )
        log(
            f"Buscar \"{args.search}\" en [{', '.join(search_fields)}]"
            f"{' (regex)' if args.search_regex else ''}: "
            f"{len(channels)}/{pre_count} canales coinciden"
        )

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
            deadline=deadline,
            max_per_source=max(0, args.max_per_source),
            require_playlist=args.require_playlist,
            resolve_variants=args.resolve_variants,
        )
        summary = Counter(check.status for check in stream_checks.values())
        counts = ", ".join(f"{status}={count}" for status, count in sorted(summary.items()))
        log(f"Checked {len(stream_checks)} stream URLs: {counts or 'none'}")
    else:
        channels = dedupe_unique_channels(
            candidate_pool,
            limit=limit if limit > 0 else None,
            exclude_keys=existing_keys if any(existing_keys) else None,
            max_per_source=max(0, args.max_per_source),
        )
        if args.require_playlist:
            log(
                "WARNING: --require-playlist necesita --check-streams; se ignora el filtro.",
                level="warning", file=sys.stderr,
            )

    suffix = args.output.suffix.lower()
    default_fmt = "csv" if suffix == ".csv" else ("m3u" if suffix in {".m3u", ".m3u8"} else "json")
    fmt = args.format or default_fmt
    write_output(channels, args.output, fmt, stream_checks=stream_checks)
    log(f"Exported {len(channels)} channels to {args.output}")

    if source_counts:
        detail = ", ".join(f"{key}={count}" for key, count in sorted(source_counts.items()))
        log(f"Canales encontrados por fuente: {detail}")
    for source_key, source_errors in sorted(errors.items()):
        # Con `--log-level DEBUG` se sacan todos; si no, los 10 primeros y se avisa del recorte
        # para que no parezca que la fuente solo tuvo ese problema.
        cap = len(source_errors) if LOG_LEVEL <= LOG_LEVELS["DEBUG"] else 10
        for error in source_errors[:cap]:
            note = error if error.startswith(f"{source_key}:") else f"{source_key}: {error}"
            log(f"WARNING: {note}", level="warning", file=sys.stderr)
        if len(source_errors) > cap:
            log(
                f"WARNING: {len(source_errors) - cap} avisos más de {source_key} ocultos "
                "(usa --log-level DEBUG para verlos).",
                level="warning", file=sys.stderr,
            )

    if not channels:
        log(
            "WARNING: no se extrajo ningún canal. Revisa los avisos de cada fuente y la "
            "conectividad del entorno.",
            "warning", file=sys.stderr,
        )

    synced: int | None = None
    sync_error = ""
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
            # Un fallo de Supabase ya no debe tirar el trabajo: el archivo sigue servido.
            sync_error = str(exc)
            log(f"ERROR: {sync_error}", level="error", file=sys.stderr)
            if args.fail_on_sync_error:
                _write_step_summary(channels, source_counts, errors, None, sync_error)
                return 1
        else:
            log(f"Sincronizados {synced} canales en Supabase ({args.activation_mode})")

    _write_step_summary(channels, source_counts, errors, synced, sync_error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
