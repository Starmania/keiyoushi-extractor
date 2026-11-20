from keiex.origins.base import site_host
from keiex.origins.keiyoushi import source_id, stable_source_id
from keiex.sitelinks import FOREVER, Range, overlapping_pairs, prefer_same_lang

K, H = "keiyoushi/extensions-source", "manga-download/haruneko"


def test_source_id_matches_gradle():
    assert source_id("AchyManga", "en") == 2311891545723109443
    # src/fr/poseidonscans sets versionId = 2
    assert source_id("Poseidon Scans", "fr", 2) == 8936307069044646761
    assert stable_source_id({"id": 8936307069044646761, "name": "Poseidon Scans", "lang": "fr"}) == str(source_id("Poseidon Scans", "fr"))


def test_stable_id_ignores_version_id():
    # src/ar/mangahub bumped its versionId: both ids are the same source.
    before = {"id": 9160891278578525988, "name": "MangaHub", "lang": "ar"}
    after = {"id": 1214953877894211809, "name": "MangaHub", "lang": "ar"}
    assert stable_source_id(before) == stable_source_id(after)


def test_stable_id_keeps_explicit_ids():
    explicit = {"id": 1557304490417397104, "name": "Kaguya", "lang": "id"}
    assert stable_source_id(explicit) == "1557304490417397104"


def test_site_host():
    assert site_host("WWW.Example.com") == "example.com"
    assert site_host("m.dumanwu1.com") == "dumanwu1.com"
    assert site_host("www.com") == "www.com"
    assert site_host("a.zazaza.me") == "a.zazaza.me"
    assert site_host("127.0.0.1") is None
    assert site_host("localhost") is None


def test_overlap_required():
    # A host reused by another site after the first one left it is no evidence.
    ranges = {"hipertoon.com": [Range(K, "1", "pt", "2026-01-01", "2026-03-01"), Range(H, "x", "pt", "2026-05-01", FOREVER)]}
    assert overlapping_pairs(ranges) == {}


def test_link_survives_domain_change():
    # Both on jmanga.cloud for a while, then haruneko moves: still one link, found on the shared host.
    ranges = {
        "jmanga.cloud": [Range(K, "1", "ja", "2026-10-12", FOREVER), Range(H, "jmanga", "ja", "2025-01-01", "2026-11-01")],
        "jmanga.party": [Range(H, "jmanga", "ja", "2026-11-01", FOREVER)],
    }
    assert overlapping_pairs(ranges) == {((K, "1", "ja"), (H, "jmanga", "ja")): ("jmanga.cloud", "2026-10-12")}


def test_multi_language_site_prefers_same_language():
    ranges = {
        "novelcool.com": [
            Range(K, "en1", "en", "2026-01-01", FOREVER),
            Range(H, "novelcool-en", "en", "2026-01-01", FOREVER),
            Range(H, "novelcool-es", "es", "2026-01-01", FOREVER),
        ]
    }
    pairs = prefer_same_lang(overlapping_pairs(ranges))
    assert list(pairs) == [((K, "en1", "en"), (H, "novelcool-en", "en"))]


def test_no_same_language_keeps_all():
    ranges = {"site.com": [Range(K, "all", "all", "2026-01-01", FOREVER), Range(H, "a", "en", "2026-01-01", FOREVER),
                           Range(H, "b", "es", "2026-01-01", FOREVER)]}
    assert len(prefer_same_lang(overlapping_pairs(ranges))) == 2
