import unittest

from sdf_tv_channels import DEFAULT_SOURCES, classify_channel, parse_m3u


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
        self.assertEqual(channels[0].source, "Test")

    def test_classifies_sports_and_language(self):
        self.assertEqual(classify_channel("ESPN Deportes", "Deportes")[:2], ("sports", "es"))

    def test_reads_language_and_country_codes(self):
        self.assertEqual(
            classify_channel(
                "Canal ejemplo",
                attrs={"tvg-language": "es", "tvg-country": "MX"},
            ),
            ("other", "es", "MX"),
        )

    def test_skips_invalid_and_orphan_urls(self):
        self.assertEqual(
            parse_m3u("#EXTM3U\nhttps://example.com/orphan.m3u8\n#EXTINF:-1,Bad\nnot-a-url\n"),
            [],
        )

    def test_deduplicates_exact_entries(self):
        text = "#EXTM3U\n#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n"
        self.assertEqual(len(parse_m3u(text)), 1)

    def test_keeps_alternate_sources(self):
        text = "#EXTM3U\n#EXTINF:-1,News TV\nhttps://example.com/a.m3u8\n#EXTINF:-1,News TV\nhttps://example.com/b.m3u8\n"
        self.assertEqual(len(parse_m3u(text)), 2)

    def test_empty_playlist(self):
        self.assertEqual(parse_m3u("#EXTM3U\n"), [])

    def test_configured_sources(self):
        urls = {s.url for s in DEFAULT_SOURCES}
        self.assertEqual(
            urls,
            {
                "https://iptv.bbyte.app/jellyfin/live.m3u",
                "https://www.cxtvenvivo.com/",
                "https://www.tdtchannels.com/lists/tv.m3u8",
                "https://m3u.cl/lista/top.m3u",
                "https://m3u.cl/lista/LATAM.m3u",
                "https://iptv-org.github.io/iptv/index.m3u",
                "https://teleonline.org/",
            },
        )


if __name__ == "__main__":
    unittest.main()
