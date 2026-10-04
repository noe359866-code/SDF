#!/usr/bin/env python3
"""Parse authorized local M3U playlists and classify their TV channels."""
from __future__ import annotations
import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

ATTR_RE = re.compile(r'([\w-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s]*)')
CATEGORY_RULES = {
    "sports": ("sport", "sports", "futbol", "football", "soccer", "deporte", "espn", "fox sports", "bein"),
    "news": ("news", "noticias", "cnn", "bbc news", "al jazeera", "euronews", "24 horas"),
    "movies": ("movie", "movies", "cinema", "cine", "pelicula", "film", "hbo", "cinemax"),
    "series": ("series", "drama", "comedy", "sitcom"),
    "kids": ("kids", "junior", "children", "infantil", "cartoon", "disney", "nickelodeon", "nick jr"),
    "documentary": ("documentary", "documental", "discovery", "history", "nat geo", "national geographic"),
    "music": ("music", "musica", "mtv", "vh1", "concert"),
    "general": ("general", "entertainment", "variedades", "television"),
}
LANGUAGE_RULES = {
    "es": ("spanish", "español", "espanol", "latino", "latina", "castellano", "esp", "deportes"),
    "en": ("english", "inglés", "ingles", "eng", "usa"),
    "pt": ("portuguese", "português", "portugues", "brasil", "brazil"),
    "fr": ("french", "français", "francais"),
    "de": ("german", "deutsch"),
    "it": ("italian", "italiano"),
    "ja": ("japanese", "japonés", "japones"),
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
    attributes: dict[str, str] | None = None

def _attributes(text: str) -> dict[str, str]:
    result = {}
    for match in ATTR_RE.finditer(text):
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace(r'\"', '"').replace(r'\\', '\\')
        result[match.group(1).lower()] = value
    return result

def _match_label(text: str, rules: dict[str, tuple[str, ...]]) -> str:
    folded = text.casefold()
    for label, keywords in rules.items():
        if any(keyword.casefold() in folded for keyword in keywords):
            return label
    return "unknown"

def classify_channel(name: str, group: str = "", attrs: dict[str, str] | None = None) -> tuple[str, str, str]:
    attrs = attrs or {}
    metadata = " ".join((attrs.get("tvg-name", ""), attrs.get("tvg-id", ""), attrs.get("tvg-language", ""), attrs.get("tvg-country", "")))
    category = _match_label(" ".join((name, group)), CATEGORY_RULES)\n    if category == "unknown":\n        category = "other"
    language_value = attrs.get("tvg-language", "").strip().lower()
    language = language_value if language_value in LANGUAGE_RULES else _match_label(" ".join((name, group, metadata)), LANGUAGE_RULES)
    country_value = attrs.get("tvg-country", "").strip().upper()
    country = country_value if country_value in COUNTRY_RULES else _match_label(" ".join((name, group, metadata)), COUNTRY_RULES)
    return category, language, country

def parse_m3u(text: str, *, deduplicate: bool = True) -> list[Channel]:
    channels = []
    pending = None
    seen = set()
    for raw_line in text.lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            header = line[len("#EXTINF:"):]
            quoted = escaped = False
            comma = -1
            for index, char in enumerate(header):
                if char == '"' and not escaped:
                    quoted = not quoted
                if char == "," and not quoted:
                    comma = index
                if char == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
            metadata, name = (header[:comma], header[comma + 1:].strip()) if comma >= 0 else (header, "")
            attrs = _attributes(metadata)
            pending = (name or attrs.get("tvg-name", "") or "Unnamed channel", attrs, attrs.get("group-title", ""))
            continue
        if line.startswith("#") or pending is None:
            continue
        name, attrs, group = pending
        parsed = urlparse(line)
        if parsed.scheme.lower() not in {"http", "https", "rtmp", "rtmps", "udp", "rtp", "rtsp", "file"}:
            pending = None
            continue
        if parsed.scheme.lower() in {"http", "https", "rtmp", "rtmps", "rtsp"} and not parsed.netloc:
            pending = None
            continue
        key = (name.casefold(), line)
        if not deduplicate or key not in seen:
            category, language, country = classify_channel(name, group, attrs)
            channels.append(Channel(name, line, group, attrs.get("tvg-id", ""), attrs.get("tvg-name", ""), attrs.get("tvg-logo", ""), language, country, category, attrs))
            seen.add(key)
        pending = None
    return channels

def read_playlist(path: Path) -> list[Channel]:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = path.read_text(encoding="latin-1")
    return parse_m3u(content)

def write_output(channels: Iterable[Channel], output: Path, fmt: str) -> None:
    rows = [asdict(channel) for channel in channels]
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        fields = ["name", "url", "group", "tvg_id", "tvg_name", "logo", "language", "country", "category"]
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract and classify channels from an authorized local M3U playlist.")
    parser.add_argument("input", type=Path, help="Local .m3u/.m3u8 playlist path")
    parser.add_argument("-o", "--output", type=Path, default=Path("channels.json"))
    parser.add_argument("--format", choices=("json", "csv"), default=None)
    parser.add_argument("--category", help="Filter by category: sports, news, movies, kids, etc.")
    parser.add_argument("--language", help="Filter by language code, e.g. es or en")
    parser.add_argument("--country", help="Filter by country code, e.g. US, MX, ES")
    args = parser.parse_args(argv)
    if not args.input.is_file():
        parser.error(f"input file does not exist: {args.input}")
    channels = read_playlist(args.input)
    if args.category:
        channels = [c for c in channels if c.category == args.category.casefold()]
    if args.language:
        channels = [c for c in channels if c.language == args.language.casefold()]
    if args.country:
        channels = [c for c in channels if c.country == args.country.upper()]
    fmt = args.format or ("csv" if args.output.suffix.lower() == ".csv" else "json")
    write_output(channels, args.output, fmt)
    print(f"Exported {len(channels)} channels to {args.output}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
