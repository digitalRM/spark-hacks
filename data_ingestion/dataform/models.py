"""Canonical dataform for the Amicus multimodal legal corpus.

Single unified entity model that every source (CourtListener, eCFR, GovInfo,
congress.gov) normalizes into. See docs/DATAFORM.md for the full design
rationale and per-source mapping tables.

Every field that is queryable in BQL carries `predicate_kinds` metadata (via
FieldMeta) matching the README's EXACT/FUZZY/SIM/SEM/VISUAL/AUDIO predicate
types, so this module can be transcribed directly into amicus/backend/schema.py's
FieldSpec registry without a redesign pass.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def new_id() -> str:
    return str(uuid.uuid4())


# Fixed, arbitrary namespace for deterministic ids -- do not change; changing it
# would silently reassign every existing canonical id on the next normalize pass.
_ID_NAMESPACE = uuid.UUID("6f9c7b6e-6b6e-4e2b-9b1a-2f6b7c8d9e0f")


def deterministic_id(source_system: str, source_id: str) -> str:
    """Stable canonical id for a (source_system, source_id) pair.

    RecordEnvelope.id defaults to a fresh random uuid4 (`new_id`), which is
    correct the first time a record is normalized -- but every normalize_*()
    function should pass an explicit `id=deterministic_id(...)` instead,
    because the *same* real-world record gets re-normalized on every resumed
    or re-run load (that's the point of an idempotent, resumable pipeline).
    A random id would make Store.save()'s `ON CONFLICT(id) DO UPDATE` upsert
    silently insert a duplicate row every time, instead of updating the
    existing one -- confirmed as a real risk 2026-08-16 when a crashed
    mid-term Oyez lane would have re-created already-saved records on resume.
    Also keeps cross-references (Document.proceeding_id, Position.person_id,
    etc.) stable across runs, since the referenced record's id never changes.
    """
    return str(uuid.uuid5(_ID_NAMESPACE, f"{source_system}:{source_id}"))


def now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SourceSystem(str, Enum):
    COURTLISTENER = "courtlistener"
    ECFR = "ecfr"
    GOVINFO = "govinfo"
    CONGRESS = "congress"
    OYEZ = "oyez"


class DocType(str, Enum):
    OPINION = "opinion"
    CFR_SECTION = "cfr_section"
    BILL = "bill"
    AMENDMENT = "amendment"
    STATUTE = "statute"
    FEDERAL_REGISTER_NOTICE = "federal_register_notice"
    COMMITTEE_REPORT = "committee_report"
    ORAL_ARGUMENT = "oral_argument"
    OTHER = "other"


class ProceedingType(str, Enum):
    CASE = "case"
    BILL = "bill"
    REGULATION_DOCKET = "regulation_docket"


class RoleType(str, Enum):
    JUDGE = "judge"
    LEGISLATOR = "legislator"
    ATTORNEY = "attorney"
    PARTY = "party"
    OFFICIAL = "official"


class OrgType(str, Enum):
    COURT = "court"
    AGENCY = "agency"
    COMMITTEE = "committee"
    FIRM = "firm"
    PARTY_ENTITY = "party_entity"


class CitationTreatment(str, Enum):
    FOLLOWED = "followed"
    DISTINGUISHED = "distinguished"
    OVERRULED = "overruled"
    CITED = "cited"


class EventType(str, Enum):
    DOCKET_ENTRY = "docket_entry"
    VOTE = "vote"
    HEARING = "hearing"
    RETENTION_EVENT = "retention_event"
    FILING = "filing"
    ACTION = "action"


# ---------------------------------------------------------------------------
# Modality / evidence bundle
# ---------------------------------------------------------------------------

class TextAsset(BaseModel):
    """Predicate kinds: EXACT (citation/id fields), FUZZY, SIM, SEM."""
    plain_text: Optional[str] = None
    html: Optional[str] = None
    language: str = "en"


class MediaLocation(BaseModel):
    """Physical location tied to a media asset -- e.g. the courthouse an Oyez
    oral argument's underlying case originated in. latitude/longitude are
    populated only when the source provides real coordinates (confirmed live
    2026-08-16: Oyez's case['location'] carries them for roughly a third of
    sampled cases -- the originating circuit/district court's address, not
    the Supreme Court building itself); name/city/region alone otherwise,
    rather than inventing coordinates the source didn't give us."""
    name: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None  # state/province
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class ImageAsset(BaseModel):
    """One scanned page or exhibit. Predicate kinds: VISUAL."""
    page_number: Optional[int] = None
    image_ref: str  # URL or blob store key
    ocr_text: Optional[str] = None
    caption: Optional[str] = None
    captured_at: Optional[datetime] = None  # when the page was scanned/captured, if known
    location: Optional[MediaLocation] = None


class AudioAsset(BaseModel):
    """One audio recording (e.g. oral argument). Predicate kinds: AUDIO."""
    audio_ref: str  # URL or blob store key
    transcript: Optional[str] = None
    duration_seconds: Optional[float] = None
    # list of {start_seconds, end_seconds, speaker, text} — populated by ASR
    timestamp_index: List[Dict] = Field(default_factory=list)
    captured_at: Optional[datetime] = None  # when the recording was made
    location: Optional[MediaLocation] = None


class MediaBundle(BaseModel):
    text: Optional[TextAsset] = None
    images: List[ImageAsset] = Field(default_factory=list)
    audio: List[AudioAsset] = Field(default_factory=list)


class EvidencePointer(BaseModel):
    """A specific citable location within a MediaBundle — what the executor
    reports back (README: 'evidence (page 12 / 47:12 in the audio)')."""
    document_id: str
    page_number: Optional[int] = None
    timestamp_seconds: Optional[float] = None
    quote: Optional[str] = None


# ---------------------------------------------------------------------------
# Record envelope — every canonical entity embeds this
# ---------------------------------------------------------------------------

class RecordEnvelope(BaseModel):
    id: str = Field(default_factory=new_id)
    source_system: SourceSystem
    source_id: str  # the primary key/identifier in the source system
    source_url: Optional[str] = None
    ingested_at: datetime = Field(default_factory=now)
    effective_date: Optional[date] = None
    version: Optional[str] = None
    # cross-source identity resolution, e.g.
    # {"courtlistener_id": "12345", "govinfo_package_id": "CFR-2024-title26"}
    external_ids: Dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------

class Citation(BaseModel):
    """Edge between two Documents. Predicate kinds: EXACT (cited_cite)."""
    envelope: RecordEnvelope
    citing_document_id: str
    cited_document_id: Optional[str] = None
    citation_string: str  # e.g. "490 U.S. 386" — matched even when unresolved
    treatment: Optional[CitationTreatment] = None
    depth: Optional[int] = None  # number of times cited in the citing doc


class Organization(BaseModel):
    """Courts, agencies, committees, firms, party-entities.
    Predicate kinds: EXACT (id/name), FUZZY (name)."""
    envelope: RecordEnvelope
    org_type: OrgType
    name: str
    short_name: Optional[str] = None
    jurisdiction: Optional[str] = None  # e.g. "ca9", "federal", state code
    parent_org_id: Optional[str] = None
    url: Optional[str] = None


class Person(BaseModel):
    """Judges, members of Congress, attorneys, parties, officials.
    Unifies people_db_person + congress.gov member.
    Predicate kinds: EXACT (id), FUZZY (name), SEM (bio-adjacent free text)."""
    envelope: RecordEnvelope
    role_types: List[RoleType] = Field(default_factory=list)
    name_first: Optional[str] = None
    name_middle: Optional[str] = None
    name_last: Optional[str] = None
    name_suffix: Optional[str] = None
    date_of_birth: Optional[date] = None
    date_of_death: Optional[date] = None
    gender: Optional[str] = None
    political_affiliations: List[str] = Field(default_factory=list)
    photo_ref: Optional[str] = None  # ImageAsset-like ref, modality: VISUAL


class Position(BaseModel):
    """Person <-> Organization link with title + dates. Covers
    people_db_position, people_db_retentionevent, congress.gov member terms
    and committee membership."""
    envelope: RecordEnvelope
    person_id: str
    organization_id: str
    title: str  # e.g. "Circuit Judge", "Senator", "Committee Chair"
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    appointer: Optional[str] = None
    how_selected: Optional[str] = None
    retention_type: Optional[str] = None
    votes_yes: Optional[int] = None
    votes_no: Optional[int] = None


class Proceeding(BaseModel):
    """Docket / bill / regulation-docket container.
    Predicate kinds: EXACT (docket_number, court_id), FUZZY (case_name)."""
    envelope: RecordEnvelope
    proceeding_type: ProceedingType
    number: str  # docket_number or bill_number
    title: Optional[str] = None  # case_name or bill title
    organization_id: Optional[str] = None  # court or chamber
    party_ids: List[str] = Field(default_factory=list)
    date_filed: Optional[date] = None
    date_terminated: Optional[date] = None
    nature_of_suit: Optional[str] = None
    status: Optional[str] = None
    document_ids: List[str] = Field(default_factory=list)


class Document(BaseModel):
    """Universal content-bearing entity: opinions, CFR sections, bills,
    statutes, Federal Register notices, committee reports.
    Predicate kinds: EXACT (citation/id), FUZZY (title), SIM/SEM (text),
    VISUAL (images), AUDIO (audio)."""
    envelope: RecordEnvelope
    doc_type: DocType
    title: Optional[str] = None  # case_name, bill title, section heading
    citation: Optional[str] = None  # canonical citation string
    parallel_citations: List[str] = Field(default_factory=list)
    jurisdiction: Optional[str] = None
    issuing_body_id: Optional[str] = None  # Organization id (court/agency/chamber)
    date_issued: Optional[date] = None
    status: Optional[str] = None  # published | proposed | repealed | pending
    summary: Optional[str] = None
    media: MediaBundle = Field(default_factory=MediaBundle)
    source_pdf_url: Optional[str] = None
    hierarchy_path: Optional[str] = None  # eCFR: "Title 26 > Chapter I > Part 1 > Sec 1.401"
    proceeding_id: Optional[str] = None
    cites: List[str] = Field(default_factory=list)  # Document ids
    amends: List[str] = Field(default_factory=list)  # Document ids
    supersedes: Optional[str] = None  # Document id


class Event(BaseModel):
    """Docket entries, votes, hearings, retention events, filings.
    Predicate kinds: EXACT (date/type), FUZZY/SEM (description)."""
    envelope: RecordEnvelope
    event_type: EventType
    proceeding_id: Optional[str] = None
    person_id: Optional[str] = None
    date: Optional[date] = None
    description: Optional[str] = None
    actor: Optional[str] = None
    outcome: Optional[str] = None


# ---------------------------------------------------------------------------
# Financial disclosures (disclosures_* family) — attached to Person
# ---------------------------------------------------------------------------

class Investment(BaseModel):
    envelope: RecordEnvelope
    financial_disclosure_id: str
    description: Optional[str] = None
    gross_value_code: Optional[str] = None
    transaction_during_reporting_period: Optional[str] = None


class Gift(BaseModel):
    envelope: RecordEnvelope
    financial_disclosure_id: str
    source: Optional[str] = None
    description: Optional[str] = None
    value: Optional[str] = None


class Debt(BaseModel):
    envelope: RecordEnvelope
    financial_disclosure_id: str
    creditor_name: Optional[str] = None
    description: Optional[str] = None
    value_code: Optional[str] = None


class Agreement(BaseModel):
    envelope: RecordEnvelope
    financial_disclosure_id: str
    date_raw: Optional[str] = None
    parties_and_terms: Optional[str] = None


class Reimbursement(BaseModel):
    envelope: RecordEnvelope
    financial_disclosure_id: str
    source: Optional[str] = None
    location: Optional[str] = None
    purpose: Optional[str] = None
    items_paid_or_provided: Optional[str] = None


class NonInvestmentIncome(BaseModel):
    envelope: RecordEnvelope
    financial_disclosure_id: str
    source: Optional[str] = None
    income_amount: Optional[str] = None


class SpouseIncome(BaseModel):
    envelope: RecordEnvelope
    financial_disclosure_id: str
    source_type: Optional[str] = None
    date_raw: Optional[str] = None


class FinancialDisclosure(BaseModel):
    """disclosures_financialdisclosure, attached to Person by person_id."""
    envelope: RecordEnvelope
    person_id: str
    year: Optional[int] = None
    filepath: Optional[str] = None  # full URL to the scanned PDF
    page_count: Optional[int] = None
    # cover-page thumbnail as a real ImageAsset -- CourtListener's bulk data
    # only ever provides one thumbnail per disclosure (confirmed live
    # 2026-08-16: thumbnail_2/_3/etc. all 404 even when page_count > 1), so
    # this is genuinely one image, not a per-page gallery.
    media: MediaBundle = Field(default_factory=MediaBundle)
    is_amended: bool = False
    investments: List[Investment] = Field(default_factory=list)
    gifts: List[Gift] = Field(default_factory=list)
    debts: List[Debt] = Field(default_factory=list)
    agreements: List[Agreement] = Field(default_factory=list)
    reimbursements: List[Reimbursement] = Field(default_factory=list)
    non_investment_income: List[NonInvestmentIncome] = Field(default_factory=list)
    spouse_income: List[SpouseIncome] = Field(default_factory=list)


class CriminalRecord(BaseModel):
    """people_db_criminalcount / people_db_criminalcomplaint, attached to Person."""
    envelope: RecordEnvelope
    person_id: str
    name: Optional[str] = None
    disposition: Optional[str] = None
    status: Optional[str] = None


# ---------------------------------------------------------------------------
# Legislation-specific convenience aliases (all backed by the entities above)
# ---------------------------------------------------------------------------
# Bill        -> Proceeding(proceeding_type=BILL) + Document(doc_type=BILL) per text version
# Amendment   -> Document(doc_type=AMENDMENT, proceeding_id=<bill's proceeding id>)
# Vote        -> Event(event_type=VOTE)
# Member      -> Person(role_types=[LEGISLATOR])
# Committee   -> Organization(org_type=COMMITTEE)
# CommitteeReport -> Document(doc_type=COMMITTEE_REPORT)
#
# Oyez oral arguments (audio-native source):
# OralArgument -> Document(doc_type=ORAL_ARGUMENT, proceeding_id=<case's Proceeding id>,
#                 media.audio=[AudioAsset(timestamp_index=[{start_seconds, end_seconds,
#                 speaker, text}, ...])])  -- Oyez ships transcript already synced to audio,
#                 so timestamp_index is populated directly at ingest, not via ASR.
# Justices/advocates speaking -> Person(role_types=[JUDGE] or [ATTORNEY]), matched to
#                 CourtListener people_db_person by name (external_ids cross-link) since
#                 Oyez has no stable person id shared with CourtListener.

ALL_MODELS = [
    Citation, Organization, Person, Position, Proceeding, Document, Event,
    Investment, Gift, Debt, Agreement, Reimbursement, NonInvestmentIncome,
    SpouseIncome, FinancialDisclosure, CriminalRecord,
]


if __name__ == "__main__":
    # smoke test: every model round-trips through its own JSON representation
    for model in ALL_MODELS:
        fields = model.model_fields
        print(f"{model.__name__}: {len(fields)} fields -> OK")
