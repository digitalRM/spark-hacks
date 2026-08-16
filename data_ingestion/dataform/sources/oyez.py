"""Oyez (oyez.org, via the public api.oyez.org backend) -> canonical dataform.

Audio-native source: oral argument recordings with a transcript already
time-aligned to speaker turns, which is exactly the AudioAsset.timestamp_index
shape the dataform needs (README: 'evidence ... 47:12 in the audio'). No API
key required; endpoints confirmed live on 2026-08-15:
  GET https://api.oyez.org/cases?filter=term:{year}&per_page=N
  GET https://api.oyez.org/cases/{term}/{docket_number}
  GET https://api.oyez.org/case_media/oral_argument_audio/{id}

This is an unofficial/undocumented API (no formal developer docs page) —
[VERIFY response shape periodically; Oyez has changed field names before].
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator, List, Optional

from dataform.config import get_json
from dataform.models import (
    AudioAsset,
    deterministic_id,
    Document,
    DocType,
    MediaLocation,
    Organization,
    OrgType,
    Person,
    Proceeding,
    ProceedingType,
    RecordEnvelope,
    RoleType,
    SourceSystem,
)

BASE_URL = "https://api.oyez.org"

# Oyez audio_detail has no separate recording-date field -- the argument date
# only exists as free text embedded in the title (e.g. "Oral Argument -
# December 02, 2019", confirmed live 2026-08-16), so this is the only real
# signal for AudioAsset.captured_at.
_TITLE_DATE_RE = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})")


def _captured_at_from_title(title: Optional[str]) -> Optional[datetime]:
    if not title:
        return None
    match = _TITLE_DATE_RE.search(title)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%B %d, %Y")
    except ValueError:
        return None


def _location_from_case(case: dict) -> Optional[MediaLocation]:
    """case['location'] is the address of the court the case was heard in --
    confirmed live 2026-08-16 to carry real lat/long for roughly a third of
    sampled cases (the originating circuit/district court, not SCOTUS itself)
    and to be entirely absent (not present-but-null-coordinates) otherwise."""
    loc = case.get("location")
    if not loc:
        return None
    return MediaLocation(
        name=loc.get("name"),
        city=loc.get("city"),
        region=loc.get("province_name") or loc.get("province"),
        latitude=loc.get("latitude"),
        longitude=loc.get("longitude"),
    )


def fetch_cases(term: str, limit: int = 20) -> List[dict]:
    data = get_json(f"{BASE_URL}/cases", params={"filter": f"term:{term}", "per_page": limit})
    return data if isinstance(data, list) else data.get("results", [])


def fetch_case_detail(term: str, docket_number: str) -> dict:
    return get_json(f"{BASE_URL}/cases/{term}/{docket_number}")


def fetch_audio_detail(audio_href: str) -> dict:
    return get_json(audio_href)


def _person_from_member(member: dict, role: RoleType) -> Person:
    person_id = str(member.get("ID"))
    name = member.get("name", "")
    parts = name.split()
    return Person(
        envelope=RecordEnvelope(
            source_system=SourceSystem.OYEZ,
            source_id=person_id,
            id=deterministic_id(SourceSystem.OYEZ.value, person_id),
            source_url=member.get("href"),
            external_ids={"oyez_identifier": member.get("identifier", "")},
        ),
        role_types=[role],
        name_first=parts[0] if parts else None,
        name_last=member.get("last_name") or (parts[-1] if parts else None),
        photo_ref=(member.get("thumbnail") or {}).get("href"),
    )


def normalize_case(case: dict) -> tuple:
    """Oyez case detail -> (Proceeding, Organization, [Person justices], [Person advocates])."""
    # heard_by/advocates entries can be null for some historical or malformed
    # cases (confirmed live 2026-08-16, crashed a full 71-term walk on an
    # unfiltered `[0]` index) -- filter out falsy entries before indexing.
    heard_by = [c for c in (case.get("heard_by") or []) if c]
    court_info = heard_by[0] if heard_by else {}
    organization = Organization(
        envelope=RecordEnvelope(
            source_system=SourceSystem.OYEZ,
            source_id=court_info.get("identifier", "scotus"),
            id=deterministic_id(SourceSystem.OYEZ.value, court_info.get("identifier", "scotus")),
            source_url=court_info.get("href"),
        ),
        org_type=OrgType.COURT,
        name=court_info.get("name", "Supreme Court of the United States"),
        jurisdiction="scotus",
    )

    proceeding = Proceeding(
        envelope=RecordEnvelope(
            source_system=SourceSystem.OYEZ,
            source_id=str(case.get("ID")),
            id=deterministic_id(SourceSystem.OYEZ.value, str(case.get("ID"))),
            source_url=case.get("href", "").replace("api.oyez.org", "www.oyez.org"),
            external_ids={"oyez_case_id": str(case.get("ID"))},
        ),
        proceeding_type=ProceedingType.CASE,
        number=case.get("docket_number", ""),
        title=case.get("name"),
        organization_id=organization.envelope.id,
    )

    justices = [
        _person_from_member(m, RoleType.JUDGE) for m in (court_info.get("members") or [])
    ]
    advocates = [
        _person_from_member(a["advocate"], RoleType.ATTORNEY)
        for a in (case.get("advocates") or [])
        if a and a.get("advocate")
    ]

    return proceeding, organization, justices, advocates


def normalize_oral_argument(case: dict, audio_detail: dict, proceeding_id: str) -> Optional[Document]:
    """One entry of case['oral_argument_audio'] (already fetched via
    fetch_audio_detail) -> Document(doc_type=ORAL_ARGUMENT)."""
    media_files = audio_detail.get("media_file") or []
    mp3 = next((f["href"] for f in media_files if f.get("mime") == "audio/mpeg"), None)
    if not mp3:
        return None

    transcript = audio_detail.get("transcript") or {}
    timestamp_index = []
    for section in transcript.get("sections", []):
        for turn in section.get("turns", []):
            speaker = (turn.get("speaker") or {}).get("name")
            text = "".join(b.get("text", "") for b in turn.get("text_blocks", []) or [])
            timestamp_index.append(
                {
                    "start_seconds": turn.get("start"),
                    "end_seconds": turn.get("stop"),
                    "speaker": speaker,
                    "text": text,
                }
            )

    doc = Document(
        envelope=RecordEnvelope(
            source_system=SourceSystem.OYEZ,
            source_id=str(audio_detail.get("id")),
            id=deterministic_id(SourceSystem.OYEZ.value, str(audio_detail.get("id"))),
            external_ids={"oyez_audio_id": str(audio_detail.get("id"))},
        ),
        doc_type=DocType.ORAL_ARGUMENT,
        title=audio_detail.get("title"),
        proceeding_id=proceeding_id,
    )
    doc.media.audio.append(
        AudioAsset(
            audio_ref=mp3,
            timestamp_index=timestamp_index,
            captured_at=_captured_at_from_title(audio_detail.get("title")),
            location=_location_from_case(case),
        )
    )
    return doc


def fetch_oral_arguments_for_case(case: dict, proceeding_id: str) -> Iterator[Document]:
    for entry in case.get("oral_argument_audio") or []:
        detail = fetch_audio_detail(entry["href"])
        doc = normalize_oral_argument(case, detail, proceeding_id)
        if doc:
            yield doc


if __name__ == "__main__":
    cases = fetch_cases(term="2019", limit=1)
    detail = fetch_case_detail("2019", cases[0]["docket_number"])
    proc, org, justices, advocates = normalize_case(detail)
    print(proc.title, "|", org.name, "| justices:", len(justices), "| advocates:", len(advocates))
    for doc in fetch_oral_arguments_for_case(detail, proc.envelope.id):
        print(doc.title, "| turns:", len(doc.media.audio[0].timestamp_index))
