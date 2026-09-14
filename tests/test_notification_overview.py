"""The research table a chat prints: one table, narrow, every value sourced."""

from dataclasses import replace

from project_pilot.enrichment.links import build_links
from project_pilot.enrichment.schemas import ContactDatum, ContactEnrichment
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.overview import overview_table, research_table, table

# The widest a value cell may get before a chat window starts scrolling sideways.
# A raw LinkedIn search URL alone is ~110 characters; the rendered text must stay
# well below that, which is what the link texts are for.
MAX_VISIBLE_CELL = 60


def _message() -> MatchMessage:
    return MatchMessage(
        title="Senior Software Engineer (m/w/d)",
        url="https://www.freelancermap.de/projekt/senior-software-engineer-m-w-d-afra-11041",
        score=82,
        listing_id=1131,
        source="freelancermap",
        origin="scan",
        status="evaluated",
        threshold=60,
        company="Randstad Digital Germany AG",
        contact_name="Talissa Blajan",
        company_page="https://www.freelancermap.de/firma/563-randstad-digital-germany-ag-randstad-digital",
        posted_at_label="14.09.2026 15:23",
    )


def _contacts() -> ContactEnrichment:
    return ContactEnrichment(
        company="Randstad Digital Germany AG",
        person="Talissa Blajan",
        website="https://www.randstaddigital.com/",
        links=build_links(company="Randstad Digital Germany AG", person="Talissa Blajan"),
        emails=[ContactDatum("laura.rossmeier@randstaddigital.com", "freelancermap")],
        phones=[
            ContactDatum("089 579 52-0", "freelancermap"),
            ContactDatum("+31 0 20 569 5911", "web"),
        ],
        persons=[ContactDatum("Talissa Blajan", "web")],
    )


def _rows(markdown: str) -> list[tuple[str, str]]:
    body = markdown.splitlines()[2:]
    return [tuple(cell.strip() for cell in line.strip("|").split(" | ", 1)) for line in body]  # type: ignore[misc]


def _visible(cell: str) -> str:
    """What a reader sees of a cell: link texts, not the URLs behind them."""
    out, rest = "", cell
    while "](" in rest:
        before, _, after = rest.partition("](")
        out += before.replace("[", "")
        rest = after.partition(")")[2]
    return out + rest


def test_contact_data_and_research_links_are_one_table() -> None:
    rendered = overview_table(_message(), _contacts())

    assert rendered.splitlines()[:2] == ["| Angabe | Wert |", "|---|---|"]
    labels = [label for label, _ in _rows(rendered)]
    assert labels == [
        "Firma",
        "Ansprechperson",
        "E-Mail",
        "Telefon",
        "Telefon",
        "Website",
        "DB",
        "Schwelle",
        "Eingestellt",
        "Firmenseite",
        "LinkedIn Firma",
        "LinkedIn Person",
        "Impressum",
    ]


def test_every_contact_value_names_where_it_came_from() -> None:
    rows = dict(_rows(overview_table(_message(), _contacts())))
    assert rows["Firma"] == "Randstad Digital Germany AG · Inserat"
    assert rows["Ansprechperson"] == "Talissa Blajan · Inserat"
    assert rows["E-Mail"] == "laura.rossmeier@randstaddigital.com · freelancermap"
    phones = [
        value
        for label, value in _rows(overview_table(_message(), _contacts()))
        if label == "Telefon"
    ]
    assert phones == ["089 579 52-0 · freelancermap", "+31 0 20 569 5911 · web"]


def test_the_table_stays_narrow_enough_not_to_scroll() -> None:
    for label, value in _rows(overview_table(_message(), _contacts())):
        assert len(label) <= 15, label
        assert len(_visible(value)) <= MAX_VISIBLE_CELL, value


def test_links_carry_a_name_and_keep_their_target() -> None:
    rows = dict(_rows(overview_table(_message(), _contacts())))
    assert rows["Website"] == "[randstaddigital.com](https://www.randstaddigital.com/)"
    assert rows["LinkedIn Person"] == (
        "[Talissa Blajan](https://www.linkedin.com/search/results/people/"
        "?keywords=Talissa+Blajan+AND+Randstad+Digital+Germany+AG)"
    )
    assert rows["Firmenseite"].startswith(
        "[freelancermap.de](https://www.freelancermap.de/firma/563-"
    )
    assert rows["Impressum"].startswith("[Google-Suche](https://www.google.com/search?q=")
    assert rows["DB"] == "1131 · freelancermap · scan · evaluated"
    assert rows["Schwelle"] == "60 (erreicht)"


def test_the_contact_person_found_again_on_the_web_is_not_listed_twice() -> None:
    labels = [label for label, _ in _rows(overview_table(_message(), _contacts()))]
    assert "Person" not in labels
    other = replace(_contacts(), persons=[ContactDatum("Laura Roßmeier", "web")])
    assert ("Person", "Laura Roßmeier · web") in _rows(overview_table(_message(), other))


def test_no_address_found_is_stated_not_dropped() -> None:
    rows = dict(_rows(overview_table(_message(), replace(_contacts(), emails=[]))))
    assert rows["E-Mail"] == "keine gefunden"


def test_a_listing_without_a_contact_says_so_in_the_person_search() -> None:
    message = replace(_message(), contact_name=None)
    rows = dict(_rows(overview_table(message, replace(_contacts(), persons=[]))))
    assert "Ansprechperson" not in rows
    assert rows["LinkedIn Person"] == "K.A."


def test_a_cell_cannot_break_the_table() -> None:
    rendered = table([("Firma", "A | B\nC"), ("Leer", "")])
    assert rendered.splitlines()[2:] == ["| Firma | A \\| B C |"]


def test_the_research_table_stands_alone_for_a_card_without_research() -> None:
    labels = [label for label, _ in _rows(research_table(_message()))]
    assert labels == [
        "DB",
        "Schwelle",
        "Eingestellt",
        "Firmenseite",
        "LinkedIn Firma",
        "LinkedIn Person",
        "Impressum",
    ]
    onsite = research_table(replace(_message(), onsite_only=True))
    assert "Vor-Ort-Projekt" in onsite
