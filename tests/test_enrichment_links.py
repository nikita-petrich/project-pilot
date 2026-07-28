"""Tests for the LinkedIn/Google research-link builders."""

from project_pilot.enrichment.links import (
    build_links,
    google_search_url,
    linkedin_company_url,
    linkedin_people_url,
)


def test_google_search_url_encodes_query() -> None:
    assert google_search_url("Muster GmbH") == "https://www.google.com/search?q=Muster+GmbH"


def test_linkedin_company_url_encodes_company() -> None:
    url = linkedin_company_url("Muster & Co")
    assert url.startswith("https://www.linkedin.com/search/results/companies/?keywords=")
    assert "Muster" in url and "Co" in url


def test_linkedin_people_url_combines_person_and_company() -> None:
    url = linkedin_people_url(company="Muster GmbH", person="Max Mustermann")
    assert "/people/" in url
    assert "Max" in url and "Muster" in url


def test_build_links_uses_company_and_person() -> None:
    links = build_links(company="Muster GmbH", person="Max Mustermann")
    assert "companies" in links.linkedin_company
    assert "people" in links.linkedin_people
    assert links.google_company.endswith("Muster+GmbH")
    assert "Impressum" in links.google_contact


def test_build_links_falls_back_to_title_when_no_company() -> None:
    links = build_links(company=None, person=None, title="Data Engineer bei ACME")
    assert "ACME" in links.google_company
    assert links.linkedin_company  # never empty
