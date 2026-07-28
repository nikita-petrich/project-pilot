"""Tests for the pure contact extractors (e-mails, phones, persons, links, scan)."""

from project_pilot.enrichment.extract import (
    deobfuscate,
    extract_emails,
    extract_persons,
    extract_phones,
    find_contact_links,
    rank_emails,
    scan_html,
)


def test_extract_emails_dedupes_lowercases_and_excludes_noise() -> None:
    text = "Info@Firma.de, info@firma.de, noreply@firma.de, logo@2x.png, real@firma.de."
    assert extract_emails(text) == ["info@firma.de", "real@firma.de"]


def test_extract_emails_excludes_platform_and_sample_addresses() -> None:
    text = "a@example.com b@sentry.io c@wixpress.com d@freelancermap.de keep@firma.de"
    assert extract_emails(text) == ["keep@firma.de"]


def test_deobfuscate_bracket_and_spelled_forms() -> None:
    assert deobfuscate("info [at] firma [dot] de") == "info@firma.de"
    assert deobfuscate("max (at) beispiel (punkt) com") == "max@beispiel.com"
    assert deobfuscate("kontakt at firma dot de") == "kontakt@firma.de"


def test_extract_emails_from_obfuscated_text() -> None:
    assert extract_emails("Schreiben Sie an info [at] firma [dot] de bitte") == ["info@firma.de"]


def test_deobfuscate_leaves_ordinary_at_in_prose() -> None:
    # " at " without the " dot <tld>" tail is not an address and must stay untouched.
    assert deobfuscate("we chat at the office") == "we chat at the office"


def test_rank_emails_prefers_person_then_role() -> None:
    emails = ["info@f.de", "max.mustermann@f.de", "bewerbung@f.de"]
    assert rank_emails(emails, "Max Mustermann")[0] == "max.mustermann@f.de"


def test_rank_emails_without_person_prefers_role_order() -> None:
    assert rank_emails(["office@f.de", "info@f.de", "bewerbung@f.de"], None) == [
        "bewerbung@f.de",
        "info@f.de",
        "office@f.de",
    ]


def test_extract_phones_handles_german_and_international_formats() -> None:
    assert extract_phones("Tel: +49 30 1234567") == ["+49 30 1234567"]
    assert extract_phones("Fon 030 / 1234-567") == ["030 1234-567"]
    assert extract_phones("(030) 1234567") == ["030 1234567"]
    assert extract_phones("0049 89 123456") == ["0049 89 123456"]


def test_extract_phones_rejects_dates_ranges_and_prices() -> None:
    assert extract_phones("Datum 12.03.2024, Zeitraum 2020-2024, Preis 1.000.000") == []


def test_extract_phones_dedupes_by_digits() -> None:
    assert extract_phones("Tel 030 1234567 oder 030 1234567") == ["030 1234567"]


def test_extract_persons_reads_impressum_labels() -> None:
    text = "Geschäftsführer: Max Mustermann. Vertreten durch Dr. Anna Schmidt."
    assert extract_persons(text) == ["Max Mustermann", "Dr. Anna Schmidt"]


def test_extract_persons_drops_company_names() -> None:
    assert extract_persons("Geschäftsführer: Muster GmbH") == []


def test_find_contact_links_classifies_and_stays_on_site() -> None:
    html = (
        '<a href="/impressum">Impressum</a>'
        '<a href="https://firma.de/kontakt">Kontakt</a>'
        '<a href="https://other.com/team">Team</a>'  # off-site: dropped
        '<a href="mailto:x@y.de">mail</a>'  # not a page: dropped
        '<a href="/karriere/jobs">Jobs</a>'
    )
    links = find_contact_links(html, "https://firma.de/")
    kinds = [(link.kind, link.url) for link in links]
    assert kinds == [
        ("impressum", "https://firma.de/impressum"),
        ("kontakt", "https://firma.de/kontakt"),
        ("karriere", "https://firma.de/karriere/jobs"),
    ]


def test_find_contact_links_orders_by_priority() -> None:
    html = '<a href="/karriere">Karriere</a><a href="/impressum">Impressum</a>'
    links = find_contact_links(html, "https://firma.de/")
    assert [link.kind for link in links] == ["impressum", "karriere"]


def test_scan_html_pulls_mailto_tel_and_text() -> None:
    html = (
        "<html><body>Kontakt"
        '<a href="mailto:info@firma.de">Mail</a>'
        '<a href="tel:+493012345">call</a>'
        " Tel: 030 987654 Geschäftsführer: Max Mustermann</body></html>"
    )
    contacts = scan_html(html)
    assert "info@firma.de" in contacts.emails
    assert "+493012345" in contacts.phones
    assert contacts.persons == ["Max Mustermann"]
