from keiex.linking import body_hosts, form_sections, name_candidates, normalize, title_names
from keiex.origins.keiyoushi import build_file_folder, folder_of, pr_number_of

FORM = """### Source information

Not Real Scans 1.6.1

### Source language

English

### Steps to reproduce

1. open https://www.notreal.example/series/x
"""


def test_folder_of():
    assert folder_of("src/fr/bluesolo/src/eu/Foo.kt") == "src/fr/bluesolo"
    assert folder_of("lib-multisrc/madara/src/Madara.kt") == "lib-multisrc/madara"
    assert folder_of("src/fr/README.md") is None
    assert folder_of("core/build.gradle.kts") is None


def test_build_file_folder():
    assert build_file_folder("src/fr/bluesolo/build.gradle.kts") == "src/fr/bluesolo"
    assert build_file_folder("lib-multisrc/madara/build.gradle.kts") == "lib-multisrc/madara"
    assert build_file_folder("src/fr/bluesolo/sub/build.gradle.kts") is None


def test_pr_number_of():
    assert pr_number_of("Aurora: open chapters (#21216)") == 21216
    assert pr_number_of("fix something") is None
    assert pr_number_of("support (#abc)") is None


def test_form_sections():
    s = form_sections(FORM)
    assert s["source information"] == "Not Real Scans 1.6.1"
    assert s["source language"] == "English"


def test_name_candidates_drop_versions():
    assert name_candidates("Not Real Scans 1.6.1")[0] == "notrealscans"
    assert name_candidates("NHentai.website v1.6.12 (EN)")[0] == "nhentaiwebsiteen"
    assert "nhentaiwebsite" in name_candidates("NHentai.website v1.6.12 (EN)")
    assert normalize("Blue Solo") == "bluesolo"


def test_body_hosts_includes_bare_domains():
    assert body_hosts(FORM) == {"www.notreal.example"}
    assert "asura.gg" in body_hosts("moved to asura.gg")


def test_title_names():
    assert title_names("[Asura Scans] chapters fail") == ["Asura Scans"]
    assert "Blue Solo" in title_names("Blue Solo: no images")
