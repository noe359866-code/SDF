import csv
import email.message
import gzip
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import sdf_tv_channels
from sdf_tv_channels import (
    DEFAULT_MAX_CHANNELS,
    DEFAULT_SOURCES,
    HlsPlaylist,
    _decode_base64_urls,
    _expand_nested_playlists,
    _fetch_text_once,
    _retry_after_seconds,
    inspect_hls_playlist,
    parse_hls_variants,
    parse_stream_headers,
    STREAM_TYPE_YOUTUBE,
    Channel,
    LinkParser,
    SourceConfig,
    StreamCheck,
    _canonical_channel_key,
    _channel_quality_score,
    _custom_sources,
    _dedupe,
    _fetch_site_page,
    _is_stream_url,
    _player_links_from_document,
    _stream_urls_from_document,
    _fetch_source,
    _fetch_text,
    _looks_like_channel_page,
    _page_matches_source,
    _player_urls_from_config,
    _script_urls_from_document,
    _sitemap_locations,
    _country_from_source_path,
    channel_slug,
    filter_channels,
    check_stream,
    check_streams,
    classify_channel,
    clean_channel_name,
    dedupe_unique_channels,
    fetch_existing_supabase_channels,
    main,
    parse_m3u,
    read_playlist,
    select_channels_with_checks,
    sync_to_supabase,
    write_output,
)


class Headers(email.message.Message):
    """Minimal stand-in for ``http.client.HTTPMessage``."""

    def __init__(self, values=None):
        super().__init__()
        for key, value in (values or {}).items():
            self[key] = value


class FakeResponse:
    def __init__(self, body, status=200, content_type="application/vnd.apple.mpegurl",
                 url="https://example.com/live.m3u8"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def geturl(self):
        return self.url


class PlaylistParserTests(unittest.TestCase):
    def test_extracts_attributes_and_name_with_comma(self):
        text = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="news.es" tvg-name="Noticias" '
            'tvg-logo="https://example.com/logo.png" group-title="News",'
            'Noticias, Internacional\n'
            'https://example.com/live.m3u8\n'
        )
        channels = parse_m3u(text, source="Test")
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "Noticias, Internacional")
        self.assertEqual(channels[0].tvg_id, "news.es")
        self.assertEqual(channels[0].category, "news")
        self.assertEqual(channels[0].country, "ES")
        self.assertEqual(channels[0].source, "Test")

    def test_reads_language_and_country_codes(self):
        self.assertEqual(
            classify_channel(
                "Canal ejemplo",
                attrs={"tvg-language": "es-MX", "tvg-country": "MEX"},
            ),
            ("other", "es", "MX"),
        )
        self.assertEqual(
            classify_channel("Canal", attrs={"tvg-country": "es-MX"})[2], "MX",
        )
        self.assertEqual(
            classify_channel("Canal", attrs={"tvg-country": "en-US"})[2], "US",
        )
        # A bare ISO country code must still be treated as a country, not a locale.
        self.assertEqual(classify_channel("Canal", attrs={"tvg-country": "ES"})[2], "ES")

    def test_language_classification_avoids_short_substring_false_positive(self):
        self.assertEqual(classify_channel("ESPN"), ("sports", "unknown", "unknown"))
        # A Spanish-language brand/group is still a useful language signal.
        self.assertEqual(classify_channel("ESPN Deportes", "Deportes")[:2], ("sports", "es"))

    def test_country_from_standard_tvg_id_suffix_and_prefix(self):
        self.assertEqual(classify_channel("CNN", attrs={"tvg-id": "CNN.us"})[2], "US")
        self.assertEqual(classify_channel("CNN", attrs={"tvg-id": "CNN.us@HD"})[2], "US")
        self.assertEqual(classify_channel("[MX] Azteca Uno")[2], "MX")

    def test_country_from_provider_channel_id_suffix_and_explicit_precedence(self):
        self.assertEqual(
            classify_channel("21 Jump Street", attrs={"channel-id": "CABB26000082J-ca"})[2],
            "CA",
        )
        self.assertEqual(
            classify_channel(
                "CNN", attrs={"channel-id": "CNN-us", "tvg-country": "GB", "tvg-id": "CNN.ca"}
            )[2],
            "GB",
        )
        # A country group is stronger than a mention in a generic channel title.
        self.assertEqual(classify_channel("21 Jump Street", "Canada")[2], "CA")

    def test_ambiguous_country_names_in_titles_do_not_override_unknown(self):
        for name in (
            "Jordan Peterson Network",
            "Georgia Bulldogs",
            "Cuba Gooding Jr",
            "The Turkey Channel",
        ):
            with self.subTest(name=name):
                self.assertEqual(classify_channel(name)[2], "unknown")
        self.assertEqual(clean_channel_name("Jordan Peterson Network"), "Jordan Peterson Network")
        # Explicit forms, including the country suffix used by TV channel titles, still work.
        self.assertEqual(classify_channel("BBC News - Jordan")[2], "JO")
        self.assertEqual(classify_channel("ESPN Colombia EN VIVO HD")[2], "CO")

    def test_supports_more_language_codes(self):
        self.assertEqual(classify_channel("Canal", attrs={"tvg-language": "ru-RU"})[1], "ru")
        self.assertEqual(classify_channel("Canal", attrs={"tvg-language": "zho"})[1], "zh")

    def test_worldwide_iso_country_codes_and_localized_names(self):
        cases = {
            "NGA": "NG",
            "VNM": "VN",
            "KOR": "KR",
            "BRA": "BR",
            "ZAF": "ZA",
            "NZL": "NZ",
            "fr-CA": "CA",
            "zh-Hant-TW": "TW",
        }
        for value, expected in cases.items():
            with self.subTest(country=value):
                self.assertEqual(
                    classify_channel("Canal", attrs={"tvg-country": value})[2], expected,
                )
        self.assertEqual(classify_channel("Canal de Sudáfrica")[2], "ZA")
        self.assertEqual(classify_channel("Canal de Côte d’Ivoire")[2], "CI")

    def test_worldwide_language_codes_and_names(self):
        cases = {
            "hau-NG": "ha",
            "ben": "bn",
            "fil": "fil",
            "szl": "szl",
            "Swahili": "sw",
            "Persian": "fa",
        }
        for value, expected in cases.items():
            with self.subTest(language=value):
                self.assertEqual(
                    classify_channel("Canal", attrs={"tvg-language": value})[1], expected,
                )
        self.assertEqual(classify_channel("Canal", attrs={"tvg-language": "und"})[1], "unknown")

    def test_category_matching_uses_word_boundaries(self):
        self.assertEqual(classify_channel("Television TV")[0], "other")
        self.assertEqual(classify_channel("Drama TV")[0], "series")

    def test_ambiguous_brand_names_are_not_misclassified(self):
        self.assertEqual(classify_channel("Mega TV")[0], "general")
        self.assertEqual(classify_channel("Canal 13C")[0], "culture")
        self.assertEqual(classify_channel("El Trece")[0], "general")
        self.assertEqual(classify_channel("Trece")[0], "other")

    def test_categories_recognize_major_world_languages(self):
        self.assertEqual(classify_channel("Canal Esportes")[0], "sports")
        self.assertEqual(classify_channel("CCTV体育频道")[0], "sports")
        self.assertEqual(classify_channel("Новости сегодня")[0], "news")
        self.assertEqual(classify_channel("Musik Fernsehen")[0], "music")

    def test_skips_invalid_and_orphan_urls(self):
        self.assertEqual(
            parse_m3u("#EXTM3U\nhttps://example.com/orphan.m3u8\n#EXTINF:-1,Bad\nnot-a-url\n"),
            [],
        )

    def test_deduplicates_exact_entries(self):
        text = (
            "#EXTM3U\n#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n"
            "#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n"
        )
        self.assertEqual(len(parse_m3u(text)), 1)

    def test_keeps_alternate_sources(self):
        text = (
            "#EXTM3U\n#EXTINF:-1,News TV\nhttps://example.com/a.m3u8\n"
            "#EXTINF:-1,News TV\nhttps://example.com/b.m3u8\n"
        )
        self.assertEqual(len(parse_m3u(text)), 2)

    def test_empty_playlist(self):
        self.assertEqual(parse_m3u("#EXTM3U\n"), [])

    def test_resolves_relative_urls_in_remote_playlists(self):
        channels = parse_m3u(
            "#EXTM3U\n#EXTINF:-1,Canal\n../streams/live.m3u8\n",
            base_url="https://example.com/lists/tv.m3u",
        )
        self.assertEqual(channels[0].url, "https://example.com/streams/live.m3u8")

    def test_supports_extgrp_group_override(self):
        channels = parse_m3u(
            "#EXTM3U\n#EXTINF:-1,Noticias 24\n#EXTGRP:Noticias\n"
            "https://example.com/news.m3u8\n"
        )
        self.assertEqual(channels[0].group, "Noticias")
        self.assertEqual(channels[0].category, "news")

    def test_does_not_parse_hls_segments_as_tv_channels(self):
        text = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
            "#EXTINF:6.0,\nsegment-1.ts\n#EXT-X-ENDLIST\n"
        )
        self.assertEqual(parse_m3u(text), [])

    def test_resolves_relative_local_playlist_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = Path(temp_dir) / "channels.m3u"
            playlist.write_text("#EXTM3U\n#EXTINF:-1,Canal\nstreams/live.ts\n", encoding="utf-8")
            channels = read_playlist(playlist)
            expected = (Path(temp_dir) / "streams" / "live.ts").resolve().as_uri()
            self.assertEqual(channels[0].url, expected)

    def test_url_dedupe_preserves_case_sensitive_paths(self):
        channels = [
            Channel("Canal", "http://EXAMPLE.com:80/Live.m3u8", source="Uno"),
            Channel("Canal", "http://example.com/Live.m3u8", source="Dos"),
            Channel("Canal", "http://example.com/live.m3u8", source="Tres"),
        ]
        unique = _dedupe(channels)
        self.assertEqual(len(unique), 2)
        self.assertIn("Uno", unique[0].source)
        self.assertIn("Dos", unique[0].source)

    def test_configured_sources(self):
        urls = {
            url
            for source in DEFAULT_SOURCES
            for url in (source.url, *source.fallback_urls)
        }
        self.assertEqual(len(DEFAULT_SOURCES), 24)
        self.assertTrue({
            "https://iptv.bbyte.app/jellyfin/live.m3u",
            "https://www.cxtvenvivo.com/",
            "https://www.tdtchannels.com/lists/tv.m3u8",
            "https://m3u.cl/lista/top.m3u",
            "https://m3u.cl/lista/LATAM.m3u",
            "https://iptv-org.github.io/iptv/index.m3u",
            "https://iptv-web.app/",
            "https://raw.githubusercontent.com/saeidrahmanbd/BDIX-IPTV/main/IPTV-Playlist.m3u",
            "https://teleonline.org/",
            "https://teleonline.github.io/listas/tv.m3u8",
            "https://www.teleonline.tv/",
            "https://www.tvenvivo.org/",
            "https://tvlibreonline.st/",
        } <= urls)
        self.assertTrue({
            "https://tvgarden.world/",
            "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/samsungtvplus_all.m3u",
            "https://www.apsattv.com/uslg.m3u",
            "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/tubi_all.m3u",
            "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/refs/heads/main/playlists/plex_all.m3u",
            "https://www.apsattv.com/vizio.m3u",
            "https://www.apsattv.com/xiaomi.m3u",
            "https://www.apsattv.com/rakutentv-uk.m3u",
            "https://www.apsattv.com/rakuten-fr.m3u",
            "https://www.apsattv.com/moviearkbr.m3u",
            "https://www.apsattv.com/cineverse.m3u",
        } <= urls)
        sources = {source.key: source for source in DEFAULT_SOURCES}
        self.assertEqual(sources["teleonline_tv"].kind, "site")
        self.assertEqual(sources["tvenvivo"].kind, "site")
        self.assertEqual(sources["tvlibreonline"].kind, "site")
        self.assertEqual(sources["tvgarden"].kind, "site")
        self.assertEqual(sources["iptv_web"].kind, "site")
        self.assertEqual(sources["iptv_web"].page_patterns, (r"^/[a-z]{2}/[^/]+/?$",))
        self.assertEqual(sources["iptv_web"].country_path_prefix, "/")
        self.assertEqual(sources["iptv_web"].sitemap_urls,
                         ("https://iptv-web.app/sitemap-index.xml",))
        self.assertEqual(
            _country_from_source_path(
                sources["iptv_web"], "https://iptv-web.app/BD/AnandaTV.bd/"
            ),
            "BD",
        )
        self.assertEqual(sources["bdix_iptv"].kind, "playlist")
        self.assertEqual(sources["tvgarden"].country_path_prefix, "/tv/")
        self.assertEqual(sources["tvgarden"].sitemap_urls,
                         ("https://tvgarden.world/sitemap_tv.xml",))
        self.assertEqual(sources["uslg"].country_hint, "US")
        self.assertEqual(sources["movieark_br"].country_hint, "BR")

    def test_remote_source_resolves_playlist_relative_to_its_url(self):
        source = SourceConfig("remote", "Remote", "https://example.com/lists/tv.m3u")
        text = "#EXTM3U\n#EXTINF:-1,Canal\n../streams/live.m3u8\n"
        with patch("sdf_tv_channels._fetch_text", return_value=text):
            channels, errors = _fetch_source(source, timeout=1, max_pages=0)
        self.assertEqual(errors, [])
        self.assertEqual(channels[0].url, "https://example.com/streams/live.m3u8")

    def test_remote_source_uses_fallback_and_applies_region_hint(self):
        source = SourceConfig(
            "regional", "Regional", "https://example.com/missing.m3u",
            fallback_urls=("https://example.com/working.m3u",), country_hint="US",
        )
        not_found = HTTPError(source.url, 404, "Not Found", None, None)
        text = (
            '#EXTM3U\n#EXTINF:-1 group-title="News",Local News\n'
            "https://cdn.example.com/news.m3u8\n"
        )
        with patch("sdf_tv_channels._fetch_text", side_effect=[not_found, text]):
            channels, errors = _fetch_source(source, timeout=1, max_pages=0)
        self.assertEqual(errors, [])
        self.assertEqual(channels[0].country, "US")
        self.assertEqual(channels[0].source_url, "https://example.com/working.m3u")

    def test_zero_max_pages_does_not_fetch_site_pages(self):
        source = SourceConfig("site", "Site", "https://example.com/", "site",
                              page_prefixes=("/tv/",))
        with patch(
            "sdf_tv_channels._fetch_text", return_value='<a href="/tv/one">One</a>'
        ) as fetch:
            channels, errors = _fetch_source(source, timeout=1, max_pages=0)
        self.assertEqual(channels, [])
        self.assertEqual(errors, [])
        fetch.assert_called_once_with(source.url, 1)

    def test_link_parser_preserves_anchor_label(self):
        parser = LinkParser()
        parser.feed('<a href="/canal/antena-3">Antena 3</a>')
        parser.close()
        self.assertEqual(parser.links, [("/canal/antena-3", "Antena 3")])

    def test_page_matching_accepts_prefixes_and_patterns(self):
        by_prefix = SourceConfig("p", "P", "https://example.com/", "site",
                                 page_prefixes=("/en-vivo/",))
        self.assertTrue(_page_matches_source(by_prefix, "/en-vivo/tn"))
        self.assertFalse(_page_matches_source(by_prefix, "/eventos/sin-chat/"))
        by_pattern = SourceConfig("q", "Q", "https://example.com/", "site",
                                  page_patterns=(r"-en-vivo(?:-online)?\.php$",))
        self.assertTrue(_page_matches_source(by_pattern, "/espn-argentina-en-vivo-online.php"))
        self.assertFalse(_page_matches_source(by_pattern, "/index.php"))
        self.assertTrue(_page_matches_source(
            SourceConfig("r", "R", "https://example.com/"), "/cualquier/cosa"))


class SitePlayerExtractionTests(unittest.TestCase):
    """Las tres fuentes nuevas publican el stream dentro de un reproductor (JS/iframe)."""

    TELEONLINE_PAGE = (
        "<html><head><title>Tyc Sports &#8211; Tele Online TV</title></head><body>"
        "<script>jQuery('#beeteam368_player_1').beeteam368_pro_player("
        r'{"video_mode":"embed","video_url":"<div class=\"teleonline-live-container\">'
        r"<iframe src=\"https:\/\/www.youtube.com\/embed\/OR8A19Hsp5w?autoplay=1"
        r'&#038;mute=1&#038;rel=0\" allowfullscreen><\/iframe><\/div>",'
        '"video_id":"OR8A19Hsp5w","post_id":4112});</script></body></html>'
    )

    TVENVIVO_PAGE = (
        "<html><head><title>ESPN COLOMBIA EN VIVO HD | TV EN VIVO</title></head><body>"
        '<h1>ESPN COLOMBIA EN VIVO</h1><iframe id="videoFrame"></iframe>'
        "<script>const SOURCES={1:\"https://www.tvenvivo.org/live/core.php?canal=espn\"};"
        "videoFrame.src=SOURCES[1];</script></body></html>"
    )
    TVENVIVO_PLAYER = '<html><body><iframe src="https://embed.example.net/player"></iframe></body></html>'
    TVENVIVO_EMBED = '<html><body><script>var s="https:\\/\\/cdn.example.com\\/hls\\/espn.m3u8";</script></body></html>'

    TVLIBRE_PAGE = (
        "<html><head><title>El Canal TN en VIVO ONLINE por Internet en DIRECTO.</title>"
        '</head><body><h1>Canal TN Online en VIVO y en directo</h1>'
        '<iframe src="https://tvlibreonline.st/html/fl/?get=VG9kb05vdGljaWFz"></iframe>'
        "</body></html>"
    )
    TVLIBRE_FL = (
        "<html><body><script>"
        "var xhttp = new XMLHttpRequest();"
        'xhttp.open("GET", "/html/cv.json?" + Math.random(), true);'
        'var urls = data["urls"];'
        'document.getElementById("iframe").src = \'//\' + randomUrl + "/cvatt.html?get=" + getVal;'
        "</script></body></html>"
    )

    def test_teleonline_tv_uses_youtube_embed_from_escaped_javascript(self):
        source = SourceConfig("teleonline_tv", "Teleonline TV", "https://www.teleonline.tv/",
                              "site", page_prefixes=("/canal/",), max_depth=1)
        page = "https://www.teleonline.tv/canal/tyc-sports/"
        with patch("sdf_tv_channels._fetch_text", return_value=self.TELEONLINE_PAGE) as fetch:
            channels, error = _fetch_site_page(source, page, "Tyc Sports", 1)
        self.assertIsNone(error)
        self.assertEqual(len(channels), 1)
        channel = channels[0]
        self.assertEqual(channel.name, "Tyc Sports")
        self.assertEqual(channel.url, "https://www.youtube.com/embed/OR8A19Hsp5w")
        self.assertEqual(channel.stream_type, STREAM_TYPE_YOUTUBE)
        self.assertEqual(channel.category, "sports")
        # El embed de YouTube no se descarga: se extrae del propio HTML.
        fetch.assert_called_once()

    def test_tvenvivo_follows_php_wrapper_to_the_final_playlist(self):
        source = SourceConfig("tvenvivo", "TV en Vivo", "https://www.tvenvivo.org/", "site",
                              max_depth=2)
        page = "https://www.tvenvivo.org/espn-argentina-en-vivo-online.php"

        def fake_fetch_with_embed(url, timeout=20, max_bytes=None, retries=2, **kwargs):
            url = str(url)
            if url.endswith(".php"):
                return self.TVENVIVO_PAGE
            if "live/core.php" in url:
                return self.TVENVIVO_PLAYER
            if "embed.example.net" in url:
                return self.TVENVIVO_EMBED
            self.fail(f"URL inesperada: {url}")

        with patch("sdf_tv_channels._fetch_text", side_effect=fake_fetch_with_embed):
            channels, error = _fetch_site_page(source, page, "", 1)
        self.assertIsNone(error)
        urls = {channel.url for channel in channels}
        self.assertIn("https://cdn.example.com/hls/espn.m3u8", urls)
        self.assertNotIn("https://www.tvenvivo.org/live/core.php?canal=espn", urls)
        channel = next(c for c in channels if c.url.endswith("espn.m3u8"))
        self.assertEqual(channel.name, "ESPN COLOMBIA")
        self.assertEqual(channel.country, "CO")
        self.assertEqual(channel.stream_type, "hls")

    def test_tvlibreonline_rebuilds_the_player_url_from_its_json_config(self):
        source = SourceConfig("tvlibreonline", "TV Libre Online", "https://tvlibreonline.st/",
                              "site", page_prefixes=("/en-vivo/",), max_depth=3)
        page = "https://tvlibreonline.st/en-vivo/tn"
        requested = []

        def fake_fetch(url, timeout=20, max_bytes=None, retries=2, **kwargs):
            url = str(url)
            requested.append(url)
            if url.endswith("/en-vivo/tn"):
                return self.TVLIBRE_PAGE
            if "/html/fl/" in url:
                return self.TVLIBRE_FL
            if "/html/cv.json" in url:
                return json.dumps({"urls": ["player1.example.net", "player2.example.net"]})
            if "cvatt.html" in url:
                # Solo el primer espejo responde; el segundo se prueba como respaldo.
                if "player1" in url:
                    return '<script>var hls="https:\\/\\/cdn2.example.com\\/live\\/tn.m3u8";</script>'
                raise URLError("mirror offline")
            self.fail(f"URL inesperada: {url}")

        with patch("sdf_tv_channels._fetch_text", side_effect=fake_fetch):
            channels, error = _fetch_site_page(source, page, "Canal TN", 1)
        self.assertIsNone(error)
        self.assertEqual([c.url for c in channels], ["https://cdn2.example.com/live/tn.m3u8"])
        self.assertEqual(channels[0].name, "Canal TN")
        played = next(url for url in requested if "cvatt.html" in url)
        # El token `get` del wrapper se conserva al reconstruir la URL del reproductor.
        self.assertTrue(played.endswith("/cvatt.html?get=VG9kb05vdGljaWFz"))

    def test_player_urls_from_config_ignores_non_player_json(self):
        payload = {"version": 3, "urls": ["cdn.example.com"], "image": "https://x/logo.png"}
        urls = _player_urls_from_config(payload, '<script>"/cvatt.html?get="</script>',
                                        "https://host/html/fl/?get=ABC")
        self.assertTrue(all("/cvatt.html?get=ABC" in url for url in urls))
        self.assertEqual(len(urls), 1)

    def test_sitemap_reader_and_script_discovery(self):
        locations = _sitemap_locations(
            '<urlset><url><loc>https://tvgarden.world/tv/us/one</loc></url></urlset>',
            "https://tvgarden.world/sitemap_tv_1.xml",
        )
        self.assertEqual(locations, ["https://tvgarden.world/tv/us/one"])
        scripts = _script_urls_from_document(
            '<script src="/assets/channels.js"></script>'
            '<script src="https://raw.githubusercontent.com/example/data.js"></script>'
            '<script src="https://www.googletagmanager.com/gtag.js"></script>',
            "https://tvgarden.world/tv/us/one",
            {"tvgarden.world", "raw.githubusercontent.com", "www.googletagmanager.com"},
        )
        self.assertEqual(scripts, [
            "https://tvgarden.world/assets/channels.js",
            "https://raw.githubusercontent.com/example/data.js",
        ])

    def test_tvgarden_sitemap_samples_multiple_countries_and_uses_route_country(self):
        source = next(source for source in DEFAULT_SOURCES if source.key == "tvgarden")
        self.assertEqual(
            _country_from_source_path(source, "https://tvgarden.world/tv/uk/uk-one"),
            "GB",
        )
        sitemap_url = source.sitemap_urls[0]
        channel_map = {
            "https://tvgarden.world/": "<html><title>TV Garden</title></html>",
            sitemap_url: (
                "<sitemapindex><sitemap><loc>"
                "https://tvgarden.world/sitemap_tv_1.xml"
                "</loc></sitemap></sitemapindex>"
            ),
            "https://tvgarden.world/sitemap_tv_1.xml": (
                "<urlset>"
                "<url><loc>https://tvgarden.world/tv/us/us-one</loc></url>"
                "<url><loc>https://tvgarden.world/tv/us/us-two</loc></url>"
                "<url><loc>https://tvgarden.world/tv/ca/ca-one</loc></url>"
                "<url><loc>https://tvgarden.world/tv/uk/uk-one</loc></url>"
                "</urlset>"
            ),
        }
        for country_code, route, title, stream, country in (
            ("us", "us-one", "France 24", "us-one", "United States"),
            ("us", "us-two", "Second US", "us-two", "United States"),
            ("ca", "ca-one", "First Canada", "ca-one", "Canada"),
            ("uk", "uk-one", "First UK", "uk-one", "United Kingdom"),
        ):
            channel_map[f"https://tvgarden.world/tv/{country_code}/{route}"] = (
                f"<html><head><title>{title} - Watch {country} TV Channel Free | "
                "tvgarden.world</title></head><body>"
                f'<video><source src="https://cdn.example.com/{stream}.m3u8"></video>'
                "</body></html>"
            )
        fetched = []

        def fake_fetch(url, timeout=20, max_bytes=None, retries=2, **kwargs):
            url = str(url)
            fetched.append(url)
            if url not in channel_map:
                self.fail(f"URL inesperada: {url}")
            return channel_map[url]

        with patch("sdf_tv_channels._fetch_text", side_effect=fake_fetch):
            channels, errors = _fetch_source(source, timeout=1, max_pages=3)
        self.assertEqual(errors, [])
        self.assertEqual({channel.name for channel in channels},
                         {"France 24", "First Canada", "First UK"})
        self.assertEqual({channel.country for channel in channels}, {"US", "CA", "GB"})
        self.assertNotIn("https://tvgarden.world/tv/us/us-two", fetched)

    def test_tvgarden_parses_country_and_stream_metadata_from_json(self):
        source = SourceConfig(
            "tvgarden", "TV Garden", "https://tvgarden.world/", "site",
            country_path_prefix="/tv/", max_depth=1,
        )
        page = "https://tvgarden.world/tv/us/channel-one"
        json_url = "https://tvgarden.world/data/us.json"
        page_html = (
            "<html><head><title>TV Garden channel</title></head><body>"
            '<script>fetch("/data/us.json")</script></body></html>'
        )
        payload = json.dumps([{
            "name": "Canal Caribe News",
            "country": "us",
            "languages": ["eng"],
            "sources": {"streams": ["https://cdn.example.com/caribe.m3u8"]},
        }])

        def fake_fetch(url, timeout=20, max_bytes=None, retries=2, **kwargs):
            if str(url) == page:
                return page_html
            if str(url) == json_url:
                return payload
            self.fail(f"URL inesperada: {url}")

        with patch("sdf_tv_channels._fetch_text", side_effect=fake_fetch):
            channels, error = _fetch_site_page(source, page, "", timeout=1)
        self.assertIsNone(error)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "Canal Caribe News")
        self.assertEqual(channels[0].country, "US")
        self.assertEqual(channels[0].language, "en")
        self.assertEqual(channels[0].source_url, page)


class DedupeAndLimitTests(unittest.TestCase):
    def test_clean_channel_name_and_canonical_key_normalize_variants(self):
        self.assertEqual(
            clean_channel_name("101. [MX] Ver Azteca Uno [1080p] [Geo-blocked] En Vivo - CXTv"),
            "Azteca Uno",
        )
        self.assertEqual(_canonical_channel_key("Antena 3 HD"), "antena-3")
        self.assertEqual(_canonical_channel_key("Antena 3 (1080p) Señal 2"), "antena-3")
        self.assertEqual(_canonical_channel_key("Antena 3 En Vivo"), "antena-3")
        self.assertEqual(_canonical_channel_key("Discovery Channel"), "discovery")
        self.assertEqual(_canonical_channel_key("Canal Discovery Latam"), "discovery-latam")
        self.assertEqual(_canonical_channel_key("Canal 24 Horas"), "24-horas")
        self.assertEqual(_canonical_channel_key("Canal 13 HD"), "canal-13")
        self.assertEqual(channel_slug("Antena 3 [720p]"), "antena-3")

    def test_region_editions_are_not_collapsed_as_quality_variants(self):
        self.assertEqual(_canonical_channel_key("Canal Discovery Latam"), "discovery-latam")
        self.assertEqual(_canonical_channel_key("CNN International"), "cnn-international")
        self.assertEqual(_canonical_channel_key("Canal Sur"), "canal-sur")
        self.assertEqual(_canonical_channel_key("Canal Norte"), "canal-norte")

        channels = [
            Channel("Canal Sur", "https://example.com/sur.m3u8", category="general"),
            Channel("Canal Norte", "https://example.com/norte.m3u8", category="general"),
            Channel("Canal Sur HD", "https://mirror.example.com/sur.m3u8", category="general"),
        ]
        selected = dedupe_unique_channels(channels, limit=10)
        self.assertEqual(
            {_canonical_channel_key(channel.name) for channel in selected},
            {"canal-sur", "canal-norte"},
        )

    def test_clean_channel_name_removes_country_labels_and_seo_tails(self):
        self.assertEqual(clean_channel_name("Argentina Telefe Ver canal"), "Telefe")
        self.assertEqual(clean_channel_name("Canal TN Online en VIVO y en directo"), "Canal TN")
        self.assertEqual(clean_channel_name("México TV Azteca"), "TV Azteca")
        self.assertEqual(clean_channel_name("Afganistán Canal Kabul"), "Canal Kabul")
        # Un nombre que depende del país se conserva intacto.
        self.assertEqual(clean_channel_name("Cuba TV"), "Cuba TV")

    def test_max_per_source_spreads_the_quota_between_sources(self):
        channels = [
            Channel(f"Canal Uno {i}", f"https://uno.example.com/{i}.m3u8",
                    category="news", source="Fuente Uno")
            for i in range(1, 6)
        ]
        channels += [
            Channel(f"Canal Dos {i}", f"https://dos.example.com/{i}.m3u8",
                    category="news", source="Fuente Dos")
            for i in range(1, 6)
        ]
        selected = dedupe_unique_channels(channels, limit=6, max_per_source=2)
        self.assertEqual(len(selected), 4)
        self.assertEqual(sum(1 for c in selected if c.name.startswith("Canal Uno")), 2)
        self.assertEqual(sum(1 for c in selected if c.name.startswith("Canal Dos")), 2)
        unlimited = dedupe_unique_channels(channels, limit=6)
        self.assertEqual(len(unlimited), 6)

    def test_stream_type_is_exported_and_marked_in_m3u(self):
        hls_channel = Channel("Canal HLS", "https://example.com/live.m3u8")
        youtube_channel = Channel("Canal YouTube", "https://www.youtube.com/embed/OR8A19Hsp5w",
                                  stream_type=STREAM_TYPE_YOUTUBE)
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "channels.json"
            csv_path = Path(temp_dir) / "channels.csv"
            m3u_path = Path(temp_dir) / "channels.m3u"

            write_output([hls_channel, youtube_channel], json_path, "json")
            rows = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual([row["stream_type"] for row in rows], ["hls", "youtube"])

            write_output([youtube_channel], csv_path, "csv")
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle))["stream_type"], "youtube")

            write_output([youtube_channel], m3u_path, "m3u")
            self.assertIn('stream-type="youtube"', m3u_path.read_text(encoding="utf-8"))
            write_output([hls_channel], m3u_path, "m3u")
            self.assertNotIn("stream-type", m3u_path.read_text(encoding="utf-8"))

    def test_same_resolved_media_playlist_collapses_duplicates(self):
        uno = Channel("Uno", "https://a.example/uno/index.m3u8")
        dos = Channel("Dos", "https://b.example/dos/master.m3u8")
        checks = {
            uno.url: StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl",
                                 media_url="https://cdn.example/shared/720.m3u8"),
            dos.url: StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl",
                                 media_url="https://cdn.example/shared/720.m3u8"),
        }
        selected = dedupe_unique_channels([uno, dos], stream_checks=checks)
        self.assertEqual(len(selected), 1)

    def test_m3u_export_roundtrips_arbitrary_headers(self):
        channel = Channel(
            "Canal", "https://cdn.example.com/x.m3u8",
            headers={"User-Agent": "OTT/1", "Referer": "https://site/", "X-Token": "abc"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.m3u"
            write_output([channel], path, "m3u")
            text = path.read_text(encoding="utf-8")
        self.assertIn("#EXTVLCOPT:http-user-agent=OTT/1", text)
        self.assertIn("#EXTVLCOPT:http-referrer=https://site/", text)
        self.assertIn('"X-Token": "abc"', text)
        self.assertEqual(parse_m3u(text)[0].headers, {
            "User-Agent": "OTT/1", "Referer": "https://site/", "X-Token": "abc",
        })

    def test_m3u_export_and_logo_sanitization(self):
        channels = parse_m3u(
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="a3.es" tvg-logo="N/A" group-title="General",Antena 3\n'
            'https://example.com/a3.m3u8\n'
            '#EXTINF:-1 tvg-id="la1.es" tvg-logo="/logos/la1.png" group-title="General",La 1\n'
            'https://example.com/la1.m3u8\n',
            base_url="https://example.com/lists/tv.m3u8",
        )
        self.assertEqual(channels[0].logo, "")
        self.assertEqual(channels[1].logo, "https://example.com/logos/la1.png")
        with tempfile.TemporaryDirectory() as temp_dir:
            m3u_path = Path(temp_dir) / "exported.m3u8"
            write_output(channels, m3u_path, "m3u")
            exported_text = m3u_path.read_text(encoding="utf-8")
            reparsed = parse_m3u(exported_text)
            self.assertEqual(len(reparsed), 2)
            self.assertEqual(reparsed[1].logo, "https://example.com/logos/la1.png")

    def test_dedupe_unique_channels_prevents_repeated_names_tvg_ids_and_urls(self):
        channels = [
            Channel("CNN", "http://example.com/cnn-low.m3u8", category="news"),
            Channel(
                "CNN HD [1080p]",
                "https://example.com/cnn-hd.m3u8",
                tvg_id="CNN.us",
                logo="https://example.com/cnn.png",
                country="US",
                language="en",
                category="news",
            ),
            Channel("CNN En Vivo", "https://example.com/cnn-mirror.m3u8", category="news"),
            Channel("Cable News Network", "https://example.com/cnn-other.m3u8", tvg_id="CNN.us", category="news"),
            Channel("ESPN Deportes", "https://example.com/espn.m3u8", category="sports", language="es"),
        ]
        unique = dedupe_unique_channels(channels, limit=20)
        self.assertEqual(len(unique), 2)
        names = {c.name for c in unique}
        self.assertEqual(names, {"CNN HD", "ESPN Deportes"})
        cnn = next(c for c in unique if "CNN" in c.name)
        self.assertEqual(cnn.url, "https://example.com/cnn-hd.m3u8")
        self.assertEqual(cnn.logo, "https://example.com/cnn.png")

    def test_limits_to_20_unique_channels_by_default(self):
        channels = [
            Channel(f"Canal Único {i}", f"https://example.com/stream-{i}.m3u8", category="news")
            for i in range(1, 35)
        ]
        # Add duplicates of each channel with HD/mirror suffixes
        channels.extend(
            Channel(f"Canal Único {i} HD [1080p]", f"https://mirror.example.com/stream-{i}.m3u8", category="news")
            for i in range(1, 35)
        )
        selected = dedupe_unique_channels(channels, limit=DEFAULT_MAX_CHANNELS)
        self.assertEqual(len(selected), 20)
        slugs = [channel_slug(c.name) for c in selected]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_checked_selection_respects_max_per_source_before_early_return(self):
        channels = [
            Channel("Alpha Uno", "https://uno.example.com/one.m3u8",
                    category="news", source="Fuente Uno"),
            Channel("Alpha Dos", "https://uno.example.com/two.m3u8",
                    category="news", source="Fuente Uno"),
            Channel("Beta Uno", "https://dos.example.com/one.m3u8",
                    category="news", source="Fuente Dos"),
            Channel("Beta Dos", "https://dos.example.com/two.m3u8",
                    category="news", source="Fuente Dos"),
        ]
        with patch("sdf_tv_channels.check_stream", return_value=StreamCheck("http_ok", 200)):
            selected, _ = select_channels_with_checks(
                channels, limit=2, only_http_ok=True, max_per_source=1,
            )
        self.assertEqual(len(selected), 2)
        self.assertEqual({channel.source for channel in selected}, {"Fuente Uno", "Fuente Dos"})

    def test_max_probes_is_a_hard_cap_even_when_below_requested_limit(self):
        channels = [
            Channel(f"Canal {i}", f"https://example.com/{i}.m3u8", category="news")
            for i in range(5)
        ]
        with patch("sdf_tv_channels.check_stream", return_value=StreamCheck("http_ok", 200)) as check:
            selected, checks = select_channels_with_checks(
                channels, limit=3, only_http_ok=True, max_probes=2,
            )
        self.assertEqual(check.call_count, 2)
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(selected), 2)

    def test_select_channels_with_checks_stops_early_and_uses_fallback_stream(self):
        channels = [
            Channel("Canal Uno", "https://example.com/uno-dead.m3u8", category="news"),
            Channel("Canal Uno HD", "https://example.com/uno-live.m3u8", category="news"),
            Channel("Canal Dos", "https://example.com/dos-live.m3u8", category="sports"),
            Channel("Canal Tres", "https://example.com/tres-live.m3u8", category="movies"),
        ]

        def fake_check(url, timeout=10, *args, **kwargs):
            if "dead" in url:
                return StreamCheck("unreachable", detail="offline")
            return StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", url)

        with patch("sdf_tv_channels.check_stream", side_effect=fake_check):
            selected, checks = select_channels_with_checks(
                channels, limit=2, timeout=1, workers=2, only_http_ok=True,
            )
        self.assertEqual(len(selected), 2)
        selected_slugs = {_canonical_channel_key(c.name) for c in selected}
        self.assertEqual(len(selected_slugs), 2)
        for ch in selected:
            self.assertEqual(checks[ch.url].status, "http_ok")

    def test_sync_to_supabase_adds_at_most_20_non_repeating_channels(self):
        channels = []
        checks = {}
        for i in range(1, 30):
            url = f"https://example.com/ch-{i}.m3u8"
            mirror_url = f"https://mirror.example.com/ch-{i}.m3u8"
            channels.append(Channel(f"Canal {i}", url, country="MX", category="general"))
            channels.append(Channel(f"Canal {i} [1080p]", mirror_url, country="MX", category="general"))
            checks[url] = StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", url)
            checks[mirror_url] = StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", mirror_url)

        captured_payload = []

        def fake_urlopen(req, timeout=30):
            captured_payload.extend(json.loads(req.data.decode("utf-8")))
            return FakeResponse(b"", status=201)

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen):
            synced = sync_to_supabase(
                channels,
                "https://project.supabase.co",
                "secret-key",
                stream_checks=checks,
                activation_mode="automatic",
            )

        self.assertEqual(synced, 20)
        self.assertEqual(len(captured_payload), 20)
        slugs = [row["slug"] for row in captured_payload]
        urls = [row["stream_url"] for row in captured_payload]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(row["is_active"] for row in captured_payload))

    def test_sync_to_supabase_skips_existing_channels_when_new_ones_available(self):
        channels = [
            Channel("Canal Existente", "https://example.com/existing.m3u8", country="ES"),
            Channel("Canal Nuevo", "https://example.com/new.m3u8", country="ES"),
        ]
        existing_keys = ({"canal-existente"}, {"https://example.com/existing.m3u8"}, {"canal-existente"})
        captured_payload = []

        def fake_urlopen(req, timeout=30):
            captured_payload.extend(json.loads(req.data.decode("utf-8")))
            return FakeResponse(b"", status=201)

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen):
            synced = sync_to_supabase(
                channels,
                "https://project.supabase.co",
                "secret-key",
                activation_mode="manual",
                existing_keys=existing_keys,
            )

        self.assertEqual(synced, 1)
        self.assertEqual(captured_payload[0]["slug"], "canal-nuevo")

    def test_fetch_existing_supabase_channels_parses_response(self):
        body = json.dumps([
            {"slug": "antena-3", "stream_url": "https://example.com/a3.m3u8", "name": "Antena 3 HD"},
        ]).encode("utf-8")
        with patch("sdf_tv_channels.urlopen", return_value=FakeResponse(body, status=200, content_type="application/json")):
            slugs, urls, names = fetch_existing_supabase_channels("https://project.supabase.co", "secret")
        self.assertIn("antena-3", slugs)
        self.assertIn("https://example.com/a3.m3u8", urls)
        self.assertIn("antena-3", names)

    def test_cli_main_exports_20_unique_channels_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = Path(temp_dir) / "input.m3u"
            output = Path(temp_dir) / "out.json"
            lines = ["#EXTM3U"]
            for i in range(1, 28):
                lines.append(f'#EXTINF:-1 group-title="News",Canal {i}')
                lines.append(f"https://example.com/live-{i}.m3u8")
                lines.append(f'#EXTINF:-1 group-title="News",Canal {i} HD [1080p]')
                lines.append(f"https://mirror.example.com/live-{i}.m3u8")
            playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")

            rc = main([str(playlist), "-o", str(output)])
            self.assertEqual(rc, 0)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(exported), 20)
            slugs = {channel_slug(item["name"]) for item in exported}
            self.assertEqual(len(slugs), 20)


class StreamCheckTests(unittest.TestCase):
    def test_recognizes_hls_and_reads_only_a_small_range(self):
        response = FakeResponse(b"\xef\xbb\xbf#EXTM3U\n#EXT-X-VERSION:3\n", status=206)
        with patch("sdf_tv_channels.urlopen", return_value=response) as open_url:
            result = check_stream("https://example.com/live.m3u8?token=abc")
        self.assertEqual(result.status, "http_ok")
        self.assertEqual(result.http_status, 206)
        self.assertIn("Playlist header detected", result.detail)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Range"), "bytes=0-4095")

    def test_retries_without_range_on_http_416(self):
        err_416 = HTTPError("https://example.com/live.m3u8", 416, "Range Not Satisfiable", {}, None)
        ok_response = FakeResponse(b"#EXTM3U\n#EXT-X-VERSION:3\n", status=200)
        with patch("sdf_tv_channels.urlopen", side_effect=[err_416, ok_response]) as open_url:
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "http_ok")
        self.assertEqual(open_url.call_count, 2)
        retry_request = open_url.call_args_list[1].args[0]
        self.assertIsNone(retry_request.get_header("Range"))

    def test_flags_html_returned_for_m3u_url(self):
        response = FakeResponse(b"<html>sign in</html>", status=200)
        with patch("sdf_tv_channels.urlopen", return_value=response):
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "invalid_playlist")

    def test_reports_restricted_http_status_without_bypassing_it(self):
        error = HTTPError("https://example.com/live.m3u8", 403, "Forbidden", {}, None)
        with patch("sdf_tv_channels.urlopen", side_effect=error):
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "restricted")
        self.assertEqual(result.http_status, 403)

    def test_reports_network_errors_and_unsupported_schemes(self):
        with patch("sdf_tv_channels.urlopen", side_effect=URLError("offline")):
            result = check_stream("https://example.com/live.m3u8")
        self.assertEqual(result.status, "unreachable")
        self.assertEqual(check_stream("rtmp://example.com/live").status, "unsupported")

    def test_check_results_are_embedded_in_json_and_csv_exports(self):
        channel = Channel("Canal", "https://example.com/live.m3u8")
        checks = {channel.url: StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl")}
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = Path(temp_dir) / "channels.json"
            csv_path = Path(temp_dir) / "channels.csv"
            write_output([channel], json_path, "json", checks)
            write_output([channel], csv_path, "csv", checks)
            json_row = json.loads(json_path.read_text(encoding="utf-8"))[0]
            self.assertEqual(json_row["stream_check"]["status"], "http_ok")
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                csv_row = next(csv.DictReader(handle))
            self.assertEqual(csv_row["stream_status"], "http_ok")
            self.assertEqual(csv_row["stream_http_status"], "200")

    def test_youtube_embed_requires_a_live_signal(self):
        live = FakeResponse(b'<html>{"isLive":true}</html>', status=200, content_type="text/html",
                            url="https://www.youtube.com/embed/OR8A19Hsp5w")
        with patch("sdf_tv_channels.urlopen", return_value=live) as open_url:
            result = check_stream("https://www.youtube.com/embed/OR8A19Hsp5w")
        self.assertEqual(result.status, "http_ok")
        self.assertIn("YouTube", result.detail)
        # Un embed no admite peticiones Range.
        self.assertIsNone(open_url.call_args.args[0].get_header("Range"))

        recorded = FakeResponse(b"<html>no live signal</html>", status=200, content_type="text/html")
        with patch("sdf_tv_channels.urlopen", return_value=recorded):
            self.assertEqual(check_stream("https://www.youtube.com/embed/OR8A19Hsp5w").status,
                             "not_live")

    def test_non_http_streams_are_returned_as_unsupported(self):
        channel = Channel("UDP Canal", "udp://239.0.0.1:1234")
        self.assertEqual(check_streams([channel])[channel.url].status, "unsupported")


class FetchTextTests(unittest.TestCase):
    """Regression tests for the HTTP download layer (compression, size, retries)."""

    class Response:
        def __init__(self, body, encoding="", charset="utf-8"):
            self.body = body
            self.headers = Headers({"Content-Encoding": encoding})
            self._charset = charset

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit=-1):
            return self.body if limit < 0 else self.body[:limit]

        def get_content_charset(self):
            return self._charset

    def test_decodes_gzip_responses(self):
        body = gzip.compress("#EXTM3U\n#EXTINF:-1,Canal\nhttps://example.com/live.m3u8\n".encode())
        with patch("sdf_tv_channels.urlopen", return_value=self.Response(body, "gzip")):
            text = _fetch_text("https://example.com/list.m3u", timeout=1, retries=0)
        self.assertIn("#EXTM3U", text)
        self.assertEqual(len(parse_m3u(text)), 1)

    def test_decodes_deflate_responses(self):
        body = zlib.compress("#EXTM3U\n#EXTINF:-1,Canal\nhttps://example.com/live.m3u8\n".encode())
        with patch("sdf_tv_channels.urlopen", return_value=self.Response(body, "deflate")):
            text = _fetch_text("https://example.com/list.m3u", timeout=1, retries=0)
        self.assertIn("#EXTM3U", text)

    def test_truncates_oversized_responses_instead_of_failing(self):
        body = b"#EXTM3U\n" + b"x" * 5000
        with patch("sdf_tv_channels.urlopen", return_value=self.Response(body)):
            text = _fetch_text("https://example.com/list.m3u", timeout=1,
                               max_bytes=1024, retries=0)
        self.assertEqual(len(text), 1024)

    def test_retries_transient_statuses(self):
        error = HTTPError("https://example.com/l.m3u", 503, "Busy", {}, None)
        body = b"#EXTM3U\n"
        with patch("sdf_tv_channels.urlopen", side_effect=[error, self.Response(body)]) as open_url:
            text = _fetch_text("https://example.com/l.m3u", timeout=1, retries=2)
        self.assertIn("#EXTM3U", text)
        self.assertEqual(open_url.call_count, 2)

    def test_raises_after_retries_are_exhausted(self):
        error = HTTPError("https://example.com/l.m3u", 404, "Not Found", {}, None)
        with patch("sdf_tv_channels.urlopen", side_effect=error):
            with self.assertRaises(HTTPError):
                _fetch_text("https://example.com/l.m3u", timeout=1, retries=1)

    def test_deadline_marks_pending_checks_as_timeout(self):
        def slow_check(url, timeout=10, *args, **kwargs):
            time.sleep(0.05)
            return StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl", url)

        channels = [Channel(f"Canal {i}", f"https://example.com/{i}.m3u8") for i in range(5)]
        with patch("sdf_tv_channels.check_stream", side_effect=slow_check):
            results = check_streams(channels, timeout=1, workers=5,
                                    deadline=time.monotonic() + 0.01)
        self.assertTrue(results)
        self.assertTrue(any(check.status == "timeout" for check in results.values()))


class ExitCodeTests(unittest.TestCase):
    """The CLI must keep working (and keep the exported file) when Supabase fails."""

    def _playlist(self, temp_dir):
        playlist = Path(temp_dir) / "input.m3u"
        playlist.write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="News",Canal Uno\nhttps://example.com/uno.m3u8\n'
            '#EXTINF:-1 group-title="Sports",Canal Dos\nhttps://example.com/dos.m3u8\n',
            encoding="utf-8",
        )
        return playlist

    def test_missing_credentials_exports_file_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            env = {k: v for k, v in os.environ.items()
                   if k not in {"SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
                                "SUPABASE_SECRET_KEY", "SUPABASE_KEY"}}
            with patch.dict(os.environ, env, clear=True):
                rc = main([str(playlist), "--sync-supabase", "-o", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)

    def test_minimum_channels_fails_safely_and_preserves_the_partial_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            with patch.dict(os.environ,
                            {"SUPABASE_URL": "https://project.supabase.co",
                             "SUPABASE_SERVICE_ROLE_KEY": "secret"}, clear=True), \
                 patch("sdf_tv_channels.fetch_existing_supabase_channels",
                       return_value=(set(), set(), set())), \
                 patch("sdf_tv_channels.sync_to_supabase", return_value=2) as sync, \
                 patch("sys.stderr", io.StringIO()) as err:
                rc = main([
                    str(playlist), "--sync-supabase", "--limit", "3", "--min-channels", "3",
                    "-o", str(output),
                ])

            self.assertEqual(rc, 1)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)
            sync.assert_not_called()
            self.assertIn("se requieren al menos 3", err.getvalue())

    def test_minimum_channels_allows_an_exact_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            rc = main([
                str(playlist), "--limit", "2", "--min-channels", "2", "-o", str(output),
            ])

            self.assertEqual(rc, 0)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)

    def test_minimum_channels_cannot_exceed_a_positive_limit(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--limit", "2", "--min-channels", "3"])
        self.assertEqual(ctx.exception.code, 2)

    def test_search_filters_channels_before_supabase_sync(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = Path(temp_dir) / "input.m3u"
            playlist.write_text(
                "#EXTM3U\n"
                '#EXTINF:-1 group-title="Sports",ESPN 2\nhttps://example.com/espn2.m3u8\n'
                '#EXTINF:-1 group-title="General",Telefe\nhttps://example.com/telefe.m3u8\n',
                encoding="utf-8",
            )
            output = Path(temp_dir) / "out.json"
            with patch.dict(os.environ,
                            {"SUPABASE_URL": "https://project.supabase.co",
                             "SUPABASE_SERVICE_ROLE_KEY": "secret"}, clear=True), \
                 patch("sdf_tv_channels.fetch_existing_supabase_channels",
                       return_value=(set(), set(), set())), \
                 patch("sdf_tv_channels.check_stream",
                       return_value=StreamCheck("http_ok", 200,
                                                "application/vnd.apple.mpegurl")), \
                 patch("sdf_tv_channels.sync_to_supabase", return_value=1) as sync:
                rc = main([str(playlist), "--search", "espn 2", "--sync-supabase",
                           "--check-streams", "--activation-mode", "automatic",
                           "--limit", "20", "-o", str(output)])

            self.assertEqual(rc, 0)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([channel["name"] for channel in exported], ["ESPN 2"])
            self.assertEqual([channel.name for channel in sync.call_args.args[0]], ["ESPN 2"])

    def test_sync_failure_does_not_abort_the_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            with patch.dict(os.environ,
                            {"SUPABASE_URL": "https://project.supabase.co",
                             "SUPABASE_SERVICE_ROLE_KEY": "secret"}, clear=True), \
                 patch("sdf_tv_channels.fetch_existing_supabase_channels",
                       return_value=(set(), set(), set())), \
                 patch("sdf_tv_channels.sync_to_supabase",
                       side_effect=RuntimeError("No se pudo sincronizar con Supabase: HTTP 401")):
                rc = main([str(playlist), "--sync-supabase", "-o", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)

    def test_sync_failure_can_still_fail_the_run_on_demand(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            with patch.dict(os.environ,
                            {"SUPABASE_URL": "https://project.supabase.co",
                             "SUPABASE_SERVICE_ROLE_KEY": "secret"}, clear=True), \
                 patch("sdf_tv_channels.fetch_existing_supabase_channels",
                       return_value=(set(), set(), set())), \
                 patch("sdf_tv_channels.sync_to_supabase",
                       side_effect=RuntimeError("boom")):
                rc = main([str(playlist), "--sync-supabase", "--fail-on-sync-error",
                           "-o", str(output)])
            self.assertEqual(rc, 1)

    def test_automatic_activation_enables_stream_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            playlist = self._playlist(temp_dir)
            output = Path(temp_dir) / "out.json"
            with patch("sdf_tv_channels.check_stream",
                       return_value=StreamCheck("http_ok", 200,
                                                "application/vnd.apple.mpegurl")) as check:
                rc = main([str(playlist), "--activation-mode", "automatic",
                           "--limit", "5", "-o", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(check.call_count, 2)

    def test_missing_input_file_is_still_a_usage_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(SystemExit) as ctx:
                main([str(Path(temp_dir) / "nope.m3u"), "-o", str(Path(temp_dir) / "o.json")])
            self.assertEqual(ctx.exception.code, 2)


class PlaylistDirectiveTests(unittest.TestCase):
    """Cabeceras de lista, grupos sueltos y nombres derivados del recurso."""

    def test_extvlcopt_and_exthttp_become_stream_headers(self):
        text = (
            "#EXTM3U\n"
            "#EXTVLCOPT:http-user-agent=OTTPlayer/1.2\n"
            "#EXTVLCOPT:http-referrer=http://site.example/\n"
            '#EXTHTTP:{"cookie":"tok=abc; sid=9"}\n'
            '#EXTINF:-1 tvg-id="uno.cl",Canal Uno\n'
            "https://cdn.example.com/uno.m3u8\n"
            "#EXTVLCOPT:http-user-agent=Otro/1\n"
            "#EXTINF:-1,Canal Dos\n"
            "https://cdn.example.com/dos.m3u8\n"
            "#EXTINF:-1,Canal Tres\n"
            "https://cdn.example.com/tres.m3u8\n"
        )
        channels = parse_m3u(text)
        leading = {
            "User-Agent": "OTTPlayer/1.2",
            "Referer": "http://site.example/",
            "Cookie": "tok=abc; sid=9",
        }
        self.assertEqual(channels[0].headers, leading)
        # El bloque declarado antes del primer canal vale para toda la lista, y una
        # directiva propia de una entrada solo sustituye la clave que declara.
        self.assertEqual(channels[1].headers, {**leading, "User-Agent": "Otro/1"})
        self.assertEqual(channels[2].headers, leading)

    def test_kodiprop_stream_headers_are_parsed(self):
        text = (
            "#EXTM3U\n"
            "#KODIPROP:inputstream.adaptive.license_type=clearkey\n"
            "#KODIPROP:inputstream.adaptive.stream_headers=User-Agent=Pay/2&Referer=http://x/\n"
            "#EXTINF:-1,Pagado\n"
            "https://cdn.example.com/pay.m3u8\n"
        )
        channel = parse_m3u(text)[0]
        self.assertEqual(channel.headers["User-Agent"], "Pay/2")
        self.assertEqual(channel.headers["Referer"], "http://x/")

    def test_parse_stream_headers_helper_ignores_unknown_options(self):
        self.assertEqual(
            parse_stream_headers([
                "#EXTVLCOPT:http-user-agent=X",
                "#EXTVLCOPT:http-ts-buffer-size=300000",
            ]),
            {"User-Agent": "X"},
        )

    def test_per_entry_directives_do_not_leak_when_the_list_has_no_global_block(self):
        text = (
            "#EXTM3U\n"
            "#EXTINF:-1,Uno\n#EXTVLCOPT:http-user-agent=Solo/Uno\nhttps://cdn.example.com/1.m3u8\n"
            "#EXTINF:-1,Dos\nhttps://cdn.example.com/2.m3u8\n"
        )
        channels = parse_m3u(text)
        # La directiva colocada dentro de la entrada solo afecta a esa entrada.
        self.assertEqual(channels[0].headers, {"User-Agent": "Solo/Uno"})
        self.assertIsNone(channels[1].headers)

    def test_standalone_extgrp_applies_to_following_entries(self):
        text = (
            "#EXTM3U\n"
            "#EXTGRP:Infantil\n"
            "#EXTINF:-1,Dibujos\n"
            "https://cdn.example.com/kids.m3u8\n"
            "#EXTINF:-1,Otro\n"
            "https://cdn.example.com/other.m3u8\n"
            "#EXTGRP:Noticias\n"
            "#EXTINF:-1,TN\n"
            "https://cdn.example.com/tn.m3u8\n"
        )
        channels = parse_m3u(text)
        self.assertEqual([channel.group for channel in channels],
                         ["Infantil", "Infantil", "Noticias"])

    def test_name_is_derived_from_the_stream_url(self):
        channels = parse_m3u(
            "#EXTM3U\n#EXTINF:-1,\nhttps://cdn.example.com/live/espn-2-hd.m3u8\n"
        )
        self.assertEqual(channels[0].name, "espn 2 hd")

    def test_opaque_hash_names_are_not_used_as_channel_names(self):
        channels = parse_m3u("#EXTM3U\n#EXTINF:-1,\nhttps://cdn.example.com/a/9f2b1c7d8e4a5b.m3u8\n")
        self.assertEqual(channels[0].name, "Unnamed channel")

    def test_master_playlist_is_not_a_list_of_channels(self):
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080\n"
            "1080.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=640x360\n"
            "360.m3u8\n"
        )
        self.assertEqual(parse_m3u(master), [])

    def test_m3u_export_roundtrips_headers_and_skips_other_group(self):
        channel = Channel(
            "Canal Uno", "https://cdn.example.com/uno.m3u8",
            headers={"User-Agent": "OTT/1", "Cookie": "a=1"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.m3u"
            write_output([channel], path, "m3u")
            text = path.read_text(encoding="utf-8")
            self.assertIn("#EXTVLCOPT:http-user-agent=OTT/1", text)
            self.assertIn('#EXTHTTP:{"cookie": "a=1"}', text)
            self.assertNotIn('group-title="other"', text)
            reparsed = parse_m3u(text)[0]
            self.assertEqual(reparsed.headers, {"User-Agent": "OTT/1", "Cookie": "a=1"})

    def test_mojibake_names_are_repaired(self):
        broken = "#EXTM3U\n#EXTINF:-1,Señal 2 Cable\nhttps://cdn.example.com/s.m3u8\n"
        self.assertEqual(parse_m3u(broken)[0].name, "Señal 2 Cable")


class HlsInspectionTests(unittest.TestCase):
    """El extractor del `.m3u8`: variantes, segmentos y estado del stream."""

    MASTER = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,CODECS=\"avc1.640028,mp4a.40.2\",NAME=\"Full HD\"\n"
        "1080.m3u8\n"
        "#EXT-X-STREAM-INF:RESOLUTION=640x360,BANDWIDTH=2000000\n"
        "360.m3u8\n"
    )

    def test_variants_are_resolved_against_the_base_and_sorted(self):
        variants = parse_hls_variants(self.MASTER, "https://cdn.example.com/live/index.m3u8")
        self.assertEqual([variant.resolution for variant in variants], ["1920x1080", "640x360"])
        self.assertEqual(variants[0].url, "https://cdn.example.com/live/1080.m3u8")
        self.assertEqual(variants[0].bandwidth, 8000000)
        # El valor entrecomillado con comas no se parte.
        self.assertEqual(variants[0].codecs, "avc1.640028,mp4a.40.2")
        self.assertEqual(variants[0].name, "Full HD")

    def test_low_resolution_wins_by_area_although_bandwidth_is_missing(self):
        text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:RESOLUTION=3840x2160\n"
            "uhd.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1280x720\n"
            "hd.m3u8\n"
        )
        variants = parse_hls_variants(text, "https://cdn.example.com/x/")
        self.assertEqual(variants[0].resolution, "3840x2160")

    def test_media_playlist_reports_segments_and_live_state(self):
        live = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:12\n"
            "#EXTINF:6.0,\nseg12.ts\n#EXTINF:6.0,\nseg13.ts\n#EXT-X-KEY:METHOD=AES-128,URI=\"k.bin\"\n"
        )
        playlist = inspect_hls_playlist(live)
        self.assertEqual(playlist.kind, "media")
        self.assertEqual(playlist.segment_count, 2)
        self.assertEqual(playlist.target_duration, 6.0)
        self.assertTrue(playlist.is_live)
        self.assertTrue(playlist.encrypted)

        vod = "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:10,\na.ts\n#EXT-X-ENDLIST\n"
        vod_info = inspect_hls_playlist(vod)
        self.assertEqual(vod_info.kind, "media")
        self.assertFalse(vod_info.is_live)

    def test_master_kind_and_non_playlist_are_distinguished(self):
        self.assertEqual(inspect_hls_playlist(self.MASTER).kind, "master")
        self.assertEqual(inspect_hls_playlist("#EXTM3U\n#EXT-X-VERSION:3\n").kind, "empty")
        self.assertEqual(inspect_hls_playlist("<html></html>").kind, "")

    def test_check_stream_resolves_master_to_the_best_variant(self):
        responses = {
            "https://cdn.example.com/live/index.m3u8": self.MASTER,
            "https://cdn.example.com/live/1080.m3u8": "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts\n",
        }

        def fake_urlopen(request, timeout=None):
            url = request.full_url if hasattr(request, "full_url") else request
            if url not in responses:
                raise AssertionError(f"URL inesperada: {url}")
            return FakeResponse(responses[url].encode(), url=url)

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen):
            check = check_stream("https://cdn.example.com/live/index.m3u8")
        self.assertEqual(check.status, "http_ok")
        self.assertEqual(check.playlist_kind, "master")
        self.assertEqual(check.media_url, "https://cdn.example.com/live/1080.m3u8")
        self.assertEqual(check.resolution, "1920x1080")
        self.assertEqual(check.segment_count, 1)
        self.assertIn("1920x1080", check.detail)

    def test_check_stream_skips_variant_resolution_when_disabled(self):
        response = FakeResponse(self.MASTER.encode(), url="https://cdn.example.com/live/index.m3u8")
        with patch("sdf_tv_channels.urlopen", return_value=response) as open_url:
            check = check_stream("https://cdn.example.com/live/index.m3u8", resolve_variants=False)
        self.assertEqual(open_url.call_count, 1)
        self.assertEqual(check.playlist_kind, "master")
        self.assertEqual(check.media_url, "https://cdn.example.com/live/1080.m3u8")

    def test_check_stream_uses_the_channel_headers_and_retries_a_blocked_probe(self):
        forbidden = HTTPError("https://cdn.example.com/x.m3u8", 403, "Forbidden", {}, None)
        playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n"

        def fake_urlopen(request, timeout=None):
            if request.get_header("User-agent") == "OTT/1":
                return FakeResponse(playlist.encode())
            raise forbidden

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen) as open_url:
            check = check_stream("https://cdn.example.com/x.m3u8", headers={"User-Agent": "OTT/1"})
        self.assertEqual(check.status, "http_ok")
        self.assertEqual(open_url.call_args_list[0].args[0].get_header("Referer"), None)

    def test_browser_agent_fallback_recovers_a_restricted_stream(self):
        def fake_urlopen(request, timeout=None):
            if request.get_header("User-agent") == "SDF-TV-Channel-Extractor/1.4":
                raise HTTPError(request.full_url, 403, "Forbidden", {}, None)
            return FakeResponse(b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n")

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen):
            check = check_stream("https://cdn.example.com/blocked.m3u8")
        self.assertEqual(check.status, "http_ok")
        self.assertEqual(check.playlist_kind, "media")

    def test_check_streams_forwards_headers_per_url(self):
        channels = [
            Channel("Uno", "https://cdn.example.com/uno.m3u8", headers={"Referer": "https://a/"}),
            Channel("Dos", "https://cdn.example.com/dos.m3u8"),
        ]
        seen = {}

        def fake_check(url, timeout=10, headers=None, resolve_variants=True):
            seen[url] = headers
            return StreamCheck("http_ok", 200)

        with patch("sdf_tv_channels.check_stream", side_effect=fake_check):
            results = check_streams(channels)
        self.assertEqual(seen["https://cdn.example.com/uno.m3u8"], {"Referer": "https://a/"})
        self.assertIsNone(seen["https://cdn.example.com/dos.m3u8"])
        self.assertEqual(results["https://cdn.example.com/uno.m3u8"].status, "http_ok")

    def test_require_playlist_drops_html_endpoints(self):
        good = Channel("Uno", "https://cdn.example.com/uno.m3u8")
        bad = Channel("Dos", "https://cdn.example.com/dos.m3u8")
        checks = {
            good.url: StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl",
                                  playlist_kind="media", segment_count=3),
            bad.url: StreamCheck("invalid_playlist", 200, "text/html"),
        }
        selected = dedupe_unique_channels([good, bad], stream_checks=checks, require_playlist=True)
        self.assertEqual([channel.name for channel in selected], ["Uno"])

    def test_quality_score_prefers_confirmed_playlists(self):
        confirmed = Channel("A", "https://cdn.example.com/a.m3u8")
        plain = Channel("A", "https://cdn.example.com/b.m3u8")
        checks = {
            confirmed.url: StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl",
                                       playlist_kind="media", segment_count=9),
            plain.url: StreamCheck("http_ok", 200, "application/vnd.apple.mpegurl"),
        }
        self.assertGreater(
            _channel_quality_score(confirmed, checks), _channel_quality_score(plain, checks),
        )


class NestedPlaylistTests(unittest.TestCase):
    """Desplegar las sub-listas `.m3u` que publica un índice."""

    INDEX = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="Deportes",Deportes\n'
        "https://m3u.cl/lista/deportes.m3u\n"
        "#EXTINF:-1,Sin sub-lista\n"
        "https://cdn.example.com/vivo.m3u8\n"
    )
    SUB = (
        "#EXTM3U\n"
        "#EXTINF:-1,Canal Uno\nhttps://cdn.example.com/uno.m3u8\n"
        "#EXTINF:-1,Canal Dos\nhttps://cdn.example.com/dos.m3u8\n"
    )

    def test_sublists_are_merged_and_the_index_entry_removed(self):
        source = SourceConfig("demo", "Demo", "https://m3u.cl/lista/top.m3u")
        channels = parse_m3u(self.INDEX, source=source.name)
        with patch("sdf_tv_channels._fetch_text", return_value=self.SUB) as fetch:
            expanded, errors = _expand_nested_playlists(
                source, channels, timeout=1, limit=5,
            )
        self.assertEqual(errors, [])
        self.assertEqual([channel.name for channel in expanded],
                         ["Sin sub-lista", "Canal Uno", "Canal Dos"])
        self.assertEqual(expanded[1].group, "Deportes")
        self.assertEqual(expanded[1].source_url, "https://m3u.cl/lista/deportes.m3u")
        fetch.assert_called_once()

    def test_limit_zero_keeps_the_index_untouched(self):
        source = SourceConfig("demo", "Demo", "https://m3u.cl/lista/top.m3u")
        channels = parse_m3u(self.INDEX, source=source.name)
        with patch("sdf_tv_channels._fetch_text") as fetch:
            expanded, errors = _expand_nested_playlists(
                source, channels, timeout=1, limit=0,
            )
        fetch.assert_not_called()
        self.assertEqual(errors, [])
        self.assertEqual(expanded, channels)

    def test_failed_sublist_keeps_its_index_entry(self):
        source = SourceConfig("demo", "Demo", "https://m3u.cl/lista/top.m3u")
        channels = parse_m3u(self.INDEX, source=source.name)
        error = URLError("offline")
        with patch("sdf_tv_channels._fetch_text", side_effect=error):
            expanded, errors = _expand_nested_playlists(
                source, channels, timeout=1, limit=5,
            )
        self.assertIn("Sin sub-lista", [channel.name for channel in expanded])
        self.assertIn("Deportes", [channel.name for channel in expanded])
        self.assertTrue(errors)

    def test_fetch_source_expands_index_and_marks_the_source(self):
        source = SourceConfig("demo", "Demo", "https://m3u.cl/lista/top.m3u")

        def fake_fetch(url, timeout=20, max_bytes=None, retries=2, headers=None, use_cache=False):
            return self.SUB if url.endswith("deportes.m3u") else self.INDEX

        with patch("sdf_tv_channels._fetch_text", side_effect=fake_fetch):
            channels, errors = _fetch_source(source, timeout=1, max_pages=0)
        self.assertEqual(errors, [])
        self.assertEqual(len(channels), 3)
        self.assertTrue(all(channel.source == "Demo" for channel in channels))


class LogLevelTests(unittest.TestCase):
    """`--log-level` existía en los workflows pero no en el CLI; ahora manda en la salida."""

    def tearDown(self):
        sdf_tv_channels.configure_logging("info")

    def test_configure_logging_accepts_names_and_numbers(self):
        self.assertEqual(sdf_tv_channels.configure_logging("DEBUG"), 10)
        self.assertEqual(sdf_tv_channels.LOG_LEVEL, 10)
        self.assertEqual(sdf_tv_channels.configure_logging("info"), 20)
        self.assertEqual(sdf_tv_channels.configure_logging(30), 30)
        with self.assertRaises(ValueError):
            sdf_tv_channels.configure_logging("verbose")

    def test_log_filters_by_level(self):
        sdf_tv_channels.configure_logging("warning")
        buffer, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", buffer), patch("sys.stderr", err):
            sdf_tv_channels.log("silencioso", "info")
            sdf_tv_channels.log("visible", "warning", file=sys.stderr)
        self.assertEqual(buffer.getvalue(), "")
        self.assertEqual(err.getvalue().strip(), "visible")

    def test_cli_accepts_log_level_case_insensitive(self):
        sdf_tv_channels.configure_logging("info")
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "l.m3u"
            playlist.write_text(
                '#EXTM3U\n#EXTINF:-1,tvg-name="T",Test\nhttp://127.0.0.1:1/x.m3u8\n',
                encoding="utf-8",
            )
            out = Path(tmp) / "out.json"
            for name in ("DEBUG", "debug"):
                with patch("sys.stdout", io.StringIO()) as buffer:
                    self.assertEqual(main(["--log-level", name, str(playlist), "-o", str(out)]), 0)
                self.assertEqual(sdf_tv_channels.LOG_LEVEL, 10)
                self.assertIn("Exported", buffer.getvalue())
            self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))), 1)

    def test_warning_level_hides_progress(self):
        sdf_tv_channels.configure_logging("warning")
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "l.m3u"
            playlist.write_text(
                '#EXTM3U\n#EXTINF:-1,tvg-name="T",Test\nhttp://127.0.0.1:1/x.m3u8\n',
                encoding="utf-8",
            )
            buffer = io.StringIO()
            with patch("sys.stdout", buffer):
                self.assertEqual(main(["--log-level", "warning", str(playlist),
                                       "-o", f"{tmp}/out.json"]), 0)
            self.assertNotIn("Exported", buffer.getvalue())

    def test_cli_rejects_unknown_log_level(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--log-level", "verbose"])
        self.assertEqual(ctx.exception.code, 2)


class SourceErrorsConsoleTests(unittest.TestCase):
    """`errors` es {fuente: [avisos]}: imprimirlo mal rompía `main` con cualquier fallo.

    El dict se recorría como si fuera una lista (`errors[:10]` → TypeError); además los
    avisos se duplicaban porque ya existía un bucle que los imprimía.

    Solo se veía en una ejecución real (las pruebas de `main` no siempre tienen avisos),
    así que aquí se fuerza el camino con un `fetch_sources` simulado.
    """

    def _run(self, log_level, errors):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            empty = Channel(name="X", url="http://127.0.0.1:1/x.m3u8")
            with patch("sdf_tv_channels.fetch_sources",
                       return_value=([empty], errors, {"demo": 1})), \
                 patch("sys.stdout", io.StringIO()) as buffer, \
                 patch("sys.stderr", io.StringIO()) as err:
                code = main(["--source", "bbyte", "-o", str(out), "--log-level", log_level])
            return code, buffer.getvalue(), err.getvalue()

    def test_warnings_print_the_source_prefix_once(self):
        code, _, err = self._run("info", {"demo": ["sin canales", "demo: timeout"]})
        self.assertEqual(code, 0)
        self.assertIn("WARNING: demo: sin canales", err)
        self.assertIn("WARNING: demo: timeout", err)
        self.assertNotIn("demo: demo:", err)
        # Cada aviso debe salir una sola vez (el bloque de consola y el antiguo bucle
        # duplicaban el mismo texto en CI).
        self.assertEqual(err.count("demo: timeout"), 1)

    def test_causes_print_before_the_no_channels_warning(self):
        # El orden importa en CI: primero las causas de cada fuente y después el
        # "no se extrajo ningún canal" (antes los avisos se imprimían al final, tras
        # la exportación y el intento de sincronizar).
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            errors = {"demo": ["demo: playlist 404", "demo: page timeout"]}
            with patch("sdf_tv_channels.fetch_sources", return_value=([], errors, {})), \
                 patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(main(["--source", "bbyte", "-o", str(out)]), 0)
            text = err.getvalue()
            self.assertLess(text.index("demo: playlist 404"),
                            text.index("no se extrajo ningún canal"))
            self.assertLess(text.index("demo: page timeout"),
                            text.index("no se extrajo ningún canal"))

    def test_debug_shows_everything(self):
        many = {"demo": [f"aviso {i}" for i in range(25)]}
        _, _, err_debug = self._run("debug", many)
        self.assertEqual(err_debug.count("WARNING: demo: aviso"), 25)
        _, _, err_info = self._run("info", many)
        self.assertEqual(err_info.count("WARNING: demo: aviso"), 10)
        self.assertIn("15 avisos más de demo ocultos", err_info)

    def test_error_level_hides_the_warnings(self):
        _, _, err = self._run("error", {"demo": ["sin canales"]})
        self.assertNotIn("WARNING: demo:", err)

    def tearDown(self):
        sdf_tv_channels.configure_logging("info")


class FilterChannelsApiTests(unittest.TestCase):
    """`filter_channels` es API pública documentada; antes no tenía ni un test."""

    CANALES = [
        Channel(name="CNN US", url="http://h/cnn.m3u8", category="news", country="US",
                language="en", source="tubi"),
        Channel(name="Canal 24h", url="http://h/24h.m3u8", category="news", country="MX",
                language="es", source="tdtchannels"),
        Channel(name="ESPN 2", url="http://h/espn.m3u8", category="sports", country="AR",
                language="es", source="tubi"),
    ]

    def test_filters_combine_in_and(self):
        self.assertEqual([c.name for c in filter_channels(self.CANALES, category="news")],
                         ["CNN US", "Canal 24h"])
        self.assertEqual([c.name for c in filter_channels(
            self.CANALES, category="news", country="US")], ["CNN US"])
        self.assertEqual([c.name for c in filter_channels(
            self.CANALES, query="cnn", category="sports")], [])

    def test_language_country_and_source(self):
        self.assertEqual([c.name for c in filter_channels(self.CANALES, language="es")],
                         ["Canal 24h", "ESPN 2"])
        self.assertEqual([c.name for c in filter_channels(self.CANALES, country="ar")],
                         ["ESPN 2"])
        self.assertEqual([c.name for c in filter_channels(self.CANALES, source="Tubi")],
                         ["CNN US", "ESPN 2"])

    def test_empty_filters_are_noops(self):
        self.assertEqual(len(filter_channels(self.CANALES)), len(self.CANALES))
        self.assertEqual(len(filter_channels(self.CANALES, query="  ", category="")),
                         len(self.CANALES))

    def test_regex_query_still_works_through_the_helper(self):
        self.assertEqual([c.name for c in filter_channels(
            self.CANALES, query=r"^(CNN|ESPN)", use_regex=True)], ["CNN US", "ESPN 2"])


class RawDirectivePreservationTests(unittest.TestCase):
    """`#EXT-X-KEY`, `#KODIPROP` y `#EXTVLCOPT` sueltos no se pierden al exportar."""

    LISTA = (
        "#EXTM3U\n"
        "#KODIPROP:inputstream.adaptive.license_type=clearkey\n"
        "#EXTVLCOPT:http-user-agent=UA-global\n"
        "#EXTINF:-1 tvg-name=\"uno\",Uno\n"
        "#EXTVLCOPT:http-proxy=http://proxy:8080\n"
        "#EXT-X-KEY:METHOD=AES-128,URI=\"https://k/k.key\"\n"
        "http://127.0.0.1:1/uno.m3u8\n"
        "#EXTINF:-1 tvg-name=\"dos\",Dos\n"
        "http://127.0.0.1:1/dos.m3u8\n"
    )

    def parse(self):
        return parse_m3u(self.LISTA, source="s", base_url="http://127.0.0.1:1/x.m3u")

    def test_unmodelled_directives_are_kept_per_entry(self):
        uno, dos = self.parse()
        joined_uno = " ".join(uno.directives)
        self.assertIn("#EXT-X-KEY:METHOD=AES-128", joined_uno)
        self.assertIn("#EXTVLCOPT:http-proxy=http://proxy:8080", joined_uno)
        # El bloque global (antes del primer #EXTINF) también afecta a la 2ª entrada.
        self.assertIn("#KODIPROP:inputstream.adaptive.license_type=clearkey", joined_uno)
        self.assertIn("#KODIPROP:inputstream.adaptive.license_type=clearkey",
                      " ".join(dos.directives))
        self.assertEqual(uno.headers["User-Agent"], "UA-global")
        self.assertNotIn("User-Agent", joined_uno)

    def test_export_reemits_raw_directives_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.m3u"
            channels = self.parse()
            write_output(channels, out, "m3u")
            text = out.read_text(encoding="utf-8")
            self.assertIn("#EXT-X-KEY:METHOD=AES-128", text)
            self.assertIn("#EXTVLCOPT:http-proxy=http://proxy:8080", text)
            # la clave de descifrado debe ir antes de la URL de su entrada
            self.assertLess(text.index("#EXT-X-KEY"), text.index("uno.m3u8"))
            again = parse_m3u(text, source="s", base_url=str(out))
            self.assertEqual([c.directives for c in again], [c.directives for c in channels])
            self.assertEqual([c.headers for c in again], [c.headers for c in channels])

    def test_channels_without_directives_stay_empty(self):
        channels = parse_m3u(
            "#EXTM3U\n#EXTINF:-1,Solo\nhttp://127.0.0.1:1/s.m3u8\n",
            source="s", base_url="http://127.0.0.1:1/x.m3u",
        )
        self.assertEqual(channels[0].directives, [])

    def test_json_export_includes_directives_and_csv_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            write_output(self.parse(), out, "json")
            rows = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(any("#EXT-X-KEY" in d for d in rows[0]["directives"]))
            csv_out = Path(tmp) / "out.csv"
            write_output(self.parse(), csv_out, "csv")
            header = csv_out.read_text(encoding="utf-8").splitlines()[0]
            self.assertNotIn("directives", header)


class PageDiscoveryFilterTests(unittest.TestCase):
    """Descubrir páginas sin perseguir assets ni tokens de un atributo `data-*`."""

    def test_assets_and_opaque_tokens_are_not_pages(self):
        for url in (
            "http://h/logo.png",
            "http://h/player.js",
            "http://h/",
            "http://h/aHR0cDovL2V4YW1wbGUuY29tL2xpdmUueC5tM3U4",
            "http://h/f/3f2b1c7d8e4a5b9c0d1e2f3a4b5c6d7e",
        ):
            self.assertFalse(_looks_like_channel_page(url), url)

    def test_numeric_ids_are_pages_not_tokens(self):
        # `/ver/2026081500012345678` es un id numérico largo: sigue siendo una página.
        for url in ("http://h/v/5452367834523678345",
                    "http://h/ver/2026081500012345678",
                    "http://h/canal/1a2b3c4d5e6f7g8h9i0jk1l2"):
            self.assertTrue(_looks_like_channel_page(url), url)

    def test_real_channel_pages_pass(self):
        for url in (
            "http://h/canal/tyc-sports",
            "http://h/espn-argentina-en-vivo-online.php",
            "http://h/tv/us/cnn-us/",
            "http://h/canal/123",
        ):
            self.assertTrue(_looks_like_channel_page(url), url)

    def test_data_stream_blobs_are_not_fetched_as_pages(self):
        import base64 as b64
        blob = b64.b64encode(b"http://127.0.0.1/live/x.m3u8").decode()
        html = f'<html><body><div data-stream="{blob}"></div><a href="/canal/uno">Uno</a></body></html>'
        source = SourceConfig("s", "S", "http://127.0.0.1/", "site")
        calls: list[str] = []

        def fake_fetch(url, timeout=20, max_bytes=None, retries=2, headers=None, use_cache=False):
            calls.append(url)
            if url.endswith("/canal/uno"):
                return "<html><title>Uno</title><script>f='/live/x.m3u8';</script></html>"
            return html

        with patch("sdf_tv_channels._fetch_text", side_effect=fake_fetch):
            channels, _errors = _fetch_source(source, timeout=1, max_pages=5)
        self.assertNotIn(f"http://127.0.0.1/{blob}", calls)
        self.assertEqual(channels[0].url, "http://127.0.0.1/live/x.m3u8")


class ObfuscatedPlayerTests(unittest.TestCase):
    """Streams ofuscados en JavaScript: base64, `document.write` y claves de reproductor."""

    def test_atob_base64_streams_are_decoded(self):
        import base64 as b64
        encoded = b64.b64encode(b"https://cdn.example.com/hls/ott/master.m3u8").decode()
        html = f'<script>var s = atob("{encoded}"); new Player(s);</script>'
        self.assertEqual(
            _stream_urls_from_document(html, "https://site.example/canal/1"),
            ["https://cdn.example.com/hls/ott/master.m3u8"],
        )
        self.assertEqual(_decode_base64_urls(html), ["https://cdn.example.com/hls/ott/master.m3u8"])

    def test_base64_in_data_attributes_is_decoded(self):
        import base64 as b64
        encoded = b64.b64encode(b"https://cdn.example.com/live/x.m3u8").decode()
        html = f'<div id="p" data-stream="{encoded}"></div>'
        self.assertEqual(
            _stream_urls_from_document(html, "https://site.example/"),
            ["https://cdn.example.com/live/x.m3u8"],
        )

    def test_document_write_iframes_and_data_iframes_are_followed(self):
        html = (
            "<script>document.write('<iframe src=\"//p2.example.com/live/core.php?c=7\"></iframe>');</script>"
            '<div data-iframe="//p.example.com/embed/42"></div>'
        )
        iframes, _js = _player_links_from_document(html, "https://site.example/canal/7")
        self.assertIn("https://p2.example.com/live/core.php?c=7", iframes)
        self.assertIn("https://p.example.com/embed/42", iframes)

    def test_player_config_keys_and_video_tags_are_scanned(self):
        html = (
            '<video><source src="/hls/canal9/master.m3u8"></video>'
            '<script>jwplayer("a").setup({sources:[{file:"/live/canal7/index.m3u8"}]});</script>'
            '<meta property="og:video" content="https://cdn.example.com/hls/og.m3u8">'
        )
        found = _stream_urls_from_document(html, "https://site.example/canal/7")
        self.assertEqual(sorted(found), [
            "https://cdn.example.com/hls/og.m3u8",
            "https://site.example/hls/canal9/master.m3u8",
            "https://site.example/live/canal7/index.m3u8",
        ])

    def test_wrapper_returning_a_master_playlist_becomes_a_channel(self):
        source = SourceConfig("w", "Wrapper", "https://site.example/", "site", max_depth=1)
        page = "https://site.example/live/core.php?c=7"
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1280x720\n"
            "https://cdn.example.com/7/720.m3u8\n"
        )

        def fake_fetch(url, timeout=20, max_bytes=None, retries=2, headers=None, use_cache=False):
            return master if url == page else "<html><title>Canal 7</title></html>"

        with patch("sdf_tv_channels._fetch_text", side_effect=fake_fetch):
            channels, error = _fetch_site_page(source, page, "Canal 7", 1)
        self.assertIsNone(error)
        self.assertEqual(channels[0].url, "https://cdn.example.com/7/720.m3u8")
        self.assertEqual(channels[0].headers["Referer"], page)
        self.assertIn("Mozilla", channels[0].headers["User-Agent"])

    def test_m3u_url_in_a_query_string_counts_as_a_stream(self):
        self.assertTrue(_is_stream_url("https://h/player.php?url=https://c/x.m3u8"))
        self.assertTrue(_is_stream_url("https://h/cdn/live/"))
        self.assertFalse(_is_stream_url("https://h/pelicula.html"))


class FetchLayerTests(unittest.TestCase):
    """Caché por ejecución, `Retry-After` y detección de codificación."""

    def test_use_cache_avoids_repeated_player_documents(self):
        body = b"#EXTM3U\n"
        with patch("sdf_tv_channels.urlopen", return_value=FetchTextTests.Response(body)) as open_url:
            first = _fetch_text("https://example.com/cfg.json", timeout=1, retries=0,
                                use_cache=True)
            second = _fetch_text("https://example.com/cfg.json", timeout=1, retries=0,
                                 use_cache=True)
        self.assertEqual(first, second)
        self.assertEqual(open_url.call_count, 1)

    def test_cache_is_not_used_by_default(self):
        body = b"#EXTM3U\n"
        with patch("sdf_tv_channels.urlopen", return_value=FetchTextTests.Response(body)) as open_url:
            _fetch_text("https://example.com/list.m3u", timeout=1, retries=0)
            _fetch_text("https://example.com/list.m3u", timeout=1, retries=0)
        self.assertEqual(open_url.call_count, 2)

    def test_cached_failure_is_reraised_as_a_value_error(self):
        error = URLError("offline")
        with patch("sdf_tv_channels.urlopen", side_effect=error):
            with self.assertRaises(URLError):
                _fetch_text("https://example.com/dead.m3u", timeout=1, retries=0, use_cache=True)
            with self.assertRaises(ValueError):
                _fetch_text("https://example.com/dead.m3u", timeout=1, retries=0, use_cache=True)

    def test_retry_after_is_bounded(self):
        headers = Headers({"Retry-After": "120"})
        self.assertEqual(_retry_after_seconds(HTTPError("https://x/y", 429, "Slow", headers, None)), 10.0)
        self.assertIsNone(_retry_after_seconds(RuntimeError("sin cabeceras")))

    def test_meta_charset_is_used_when_the_server_declares_nothing(self):
        body = "#EXTM3U\n#EXTINF:-1,Señal Latinada\nhttps://x/y.m3u8\n".encode("latin-1")

        class Raw:
            headers = Headers({})

            def __enter__(self): return self
            def __exit__(self, *_a): return None
            def read(self, limit=-1): return body

        with patch("sdf_tv_channels.urlopen", return_value=Raw()):
            text = _fetch_text_once("https://example.com/l.m3u", 1, 4096, None)
        self.assertIn("Señal Latinada", text)

    def test_source_headers_are_sent_for_configured_sources(self):
        source = SourceConfig("h", "H", "https://example.com/l.m3u",
                              headers=("X-Token: abc", "Referer: https://example.com/"))
        captured = {}

        class Raw:
            headers = Headers({"Content-Type": "text/plain"})

            def __enter__(self): return self
            def __exit__(self, *_a): return None
            def read(self, limit=-1): return b"#EXTM3U\n"

        def fake_urlopen(request, timeout=None):
            captured["token"] = request.get_header("X-token")
            captured["referer"] = request.get_header("Referer")
            return Raw()

        with patch("sdf_tv_channels.urlopen", side_effect=fake_urlopen):
            _fetch_source(source, timeout=1, max_pages=0)
        self.assertEqual(captured["token"], "abc")
        self.assertEqual(captured["referer"], "https://example.com/")


class CliSourceUrlTests(unittest.TestCase):
    """`--source-url`: fuentes puntuales desde la CLI."""

    def test_adhoc_playlist_url_is_fetched_and_classified(self):
        playlist = (
            '#EXTM3U\n#EXTINF:-1 group-title="Noticias",Canal Noticias\n'
            "https://cdn.example.com/news.m3u8\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "out.json"
            with patch("sdf_tv_channels._fetch_text", return_value=playlist):
                rc = main(["--source-url", "https://m3u.cl/lista/news.m3u",
                           "-o", str(output)])
            self.assertEqual(rc, 0)
            rows = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(rows[0]["category"], "news")
        self.assertEqual(rows[0]["country"], "CL")

    def test_adhoc_site_url_is_treated_as_a_site_source(self):
        sources = _custom_sources(["tv.example.com/en-vivo/"])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].kind, "site")
        # Sin filtros de ruta: `--max-pages` acota el rastreo.
        self.assertEqual(sources[0].page_patterns, ())
        self.assertEqual(sources[0].page_prefixes, ())

    def test_adhoc_source_infers_the_country_from_the_cc_tld(self):
        self.assertEqual(_custom_sources(["https://m3u.cl/lista/top.m3u"])[0].country_hint, "CL")
        # Un dominio genérico o un "domain hack" no deben inventar un país.
        self.assertEqual(_custom_sources(["https://tvgarden.world/"])[0].country_hint, "")
        self.assertEqual(_custom_sources(["https://teleonline.tv/"])[0].country_hint, "")

    def test_adhoc_playlist_entries_are_capped_and_source_named(self):
        sources = _custom_sources(
            ["https://a.example.com/x.m3u", "https://b.example.com/", "nope"],
        )
        self.assertEqual([source.key for source in sources],
                         ["custom-a-example-com", "custom2-b-example-com"])
        self.assertEqual(sources[0].kind, "playlist")
        self.assertEqual(sources[1].url, "https://b.example.com/")

    def test_custom_source_names_are_used_as_the_channel_group(self):
        sources = _custom_sources(["https://tv.example.com/"], name="TV Example")
        self.assertEqual(sources[0].name, "TV Example")
        self.assertEqual(sources[0].key, "custom-tv-example-com")



if __name__ == "__main__":
    unittest.main()
