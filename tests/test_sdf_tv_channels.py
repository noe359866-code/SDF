import unittest
from sdf_tv_channels import classify_channel, parse_m3u

class PlaylistParserTests(unittest.TestCase):
    def test_extracts_attributes_and_name_with_comma(self):
        playlist = '#EXTM3U\n#EXTINF:-1 tvg-id="news.es" tvg-name="Noticias" tvg-logo="https://example.com/logo.png" group-title="News",Noticias, Internacional\nhttps://example.com/live.m3u8\n'
        channels = parse_m3u(playlist)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "Noticias, Internacional")
        self.assertEqual(channels[0].tvg_id, "news.es")
        self.assertEqual(channels[0].category, "news")
    def test_classifies_sports_and_language(self):
        category, language, _ = classify_channel("ESPN Deportes", "Deportes")
        self.assertEqual(category, "sports")
        self.assertEqual(language, "es")
    def test_skips_invalid_and_orphan_urls(self):
        self.assertEqual(parse_m3u("#EXTM3U\nhttps://example.com/orphan.m3u8\n#EXTINF:-1,Bad\nnot-a-url\n"), [])
    def test_deduplicates_exact_entries(self):
        text = "#EXTM3U\n#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n#EXTINF:-1,Sports TV\nhttps://example.com/a.m3u8\n"
        self.assertEqual(len(parse_m3u(text)), 1)
    def test_keeps_alternate_sources(self):
        text = "#EXTM3U\n#EXTINF:-1,News TV\nhttps://example.com/a.m3u8\n#EXTINF:-1,News TV\nhttps://example.com/b.m3u8\n"
        self.assertEqual(len(parse_m3u(text)), 2)
    def test_empty_playlist(self):
        self.assertEqual(parse_m3u("#EXTM3U\n"), [])

if __name__ == "__main__":
    unittest.main()
