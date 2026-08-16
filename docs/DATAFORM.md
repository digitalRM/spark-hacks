# The Amicus Dataform

**A single canonical record format that CourtListener, eCFR, GovInfo, congress.gov, and Oyez all normalize into — so BQL can query case law, regulations, legislation, and oral argument audio as one corpus.**

Reference implementation: [`dataform/models.py`](../dataform/models.py) (Pydantic). Loaders: [`dataform/sources/`](../dataform/sources/). Storage: [`dataform/store.py`](../dataform/store.py) (SQLite).

---

## 1. Design principles

1. **One canonical entity model.** Courts, regulations, and legislation all map into the same ~10 entities. A BQL query can join a `Document` from CourtListener against a `Document` from eCFR without a translation layer, because they're the same type.
2. **Every content-bearing entity carries a modality bundle.** `Document.media` is a `MediaBundle{text, images[], audio[]}` regardless of source — a CFR section and a SCOTUS oral argument use the identical shape, just populate different parts of it.
3. **Every field declares its predicate kind.** Matching the README's optimizer (`EXACT | FUZZY | SIM | SEM | VISUAL | AUDIO`), each field below is tagged with what kind of BQL predicate it supports, so this doc transcribes directly into a `FieldSpec` registry without a redesign pass.
4. **Cross-source identity via `external_ids`.** The same statute can arrive from GovInfo *and* congress.gov; the same case can arrive from CourtListener *and* Oyez. Every record carries an `external_ids: Dict[str,str]` map (e.g. `{"courtlistener_cluster_id": "9083418"}`) so records can be linked or de-duplicated instead of silently double-counted.
5. **Evidence pointers, not blobs.** The executor needs to cite *where* — `page 12`, `47:12 in the audio` — not just *that* a document matched. `EvidencePointer{document_id, page_number, timestamp_seconds, quote}` is the return shape for that.

---

## 2. Predicate-kind legend

| Kind | Meaning | Typical fields |
|---|---|---|
| `EXACT` | SQL equality/range, near-zero cost | ids, citation strings, dates, enums |
| `FUZZY` | string similarity, cheap | names, titles |
| `SIM` | embedding similarity / retrieval | free text bodies |
| `SEM` | LLM semantic judgement | free text bodies, summaries |
| `VISUAL` | vision-model predicate | `ImageAsset` fields |
| `AUDIO` | audio/transcript predicate | `AudioAsset` fields |

---

## 3. Canonical entity model

### RecordEnvelope — embedded in every entity below
| Field | Type | Predicate kinds |
|---|---|---|
| `id` | uuid | `EXACT` |
| `source_system` | enum: courtlistener \| ecfr \| govinfo \| congress \| oyez | `EXACT` |
| `source_id` | string | `EXACT` |
| `source_url` | string? | — |
| `ingested_at` | datetime | `EXACT` |
| `effective_date` | date? | `EXACT` |
| `version` | string? | `EXACT` |
| `external_ids` | map\<string,string\> | `EXACT` (per key) |

### MediaBundle — the modality payload on `Document`
| Field | Type | Predicate kinds |
|---|---|---|
| `text.plain_text` / `text.html` | string? | `SIM`, `SEM` |
| `images[].image_ref`, `.page_number`, `.ocr_text`, `.caption` | list | `VISUAL` (image_ref/caption), `SIM`/`SEM` (ocr_text) |
| `audio[].audio_ref`, `.transcript`, `.duration_seconds`, `.timestamp_index` | list | `AUDIO` |

### Document — the universal content entity
Opinions, CFR sections, bills, statutes, Federal Register notices, committee reports, oral arguments.
| Field | Type | Predicate kinds |
|---|---|---|
| `doc_type` | enum (opinion, cfr_section, bill, amendment, statute, federal_register_notice, committee_report, oral_argument, other) | `EXACT` |
| `title` | string? | `FUZZY`, `SEM` |
| `citation` / `parallel_citations[]` | string | `EXACT` |
| `jurisdiction` | string? | `EXACT` |
| `issuing_body_id` | → Organization | `EXACT` (join) |
| `date_issued` | date? | `EXACT` |
| `status` | string? | `EXACT` |
| `summary` | string? | `SIM`, `SEM` |
| `media` | MediaBundle | see above |
| `source_pdf_url` | string? | — |
| `hierarchy_path` | string? (eCFR: `"Title 26 > Ch. I > Part 1 > §1.401"`) | `EXACT`, `FUZZY` |
| `proceeding_id` | → Proceeding | `EXACT` (join) |
| `cites[]` / `amends[]` / `supersedes` | → Document | `EXACT` (join) |

### Proceeding — docket / bill / regulation-docket container
| Field | Type | Predicate kinds |
|---|---|---|
| `proceeding_type` | enum (case, bill, regulation_docket) | `EXACT` |
| `number` | string (docket # or bill #) | `EXACT` |
| `title` | string? | `FUZZY`, `SEM` |
| `organization_id` | → Organization (court/chamber) | `EXACT` (join) |
| `party_ids[]` | → Person | `EXACT` (join) |
| `date_filed` / `date_terminated` | date? | `EXACT` |
| `nature_of_suit` | string? | `EXACT`, `FUZZY` |
| `status` | string? | `EXACT` |
| `document_ids[]` | → Document | `EXACT` (join) |

### Person — judges, legislators, attorneys, parties, officials
| Field | Type | Predicate kinds |
|---|---|---|
| `role_types[]` | enum list (judge, legislator, attorney, party, official) | `EXACT` |
| `name_first/middle/last/suffix` | string? | `FUZZY` |
| `date_of_birth` / `date_of_death` | date? | `EXACT` |
| `gender` | string? | `EXACT` |
| `political_affiliations[]` | string list | `EXACT` |
| `photo_ref` | string? | `VISUAL` |

### Position — Person↔Organization link
`title`, `date_start`, `date_end`, `appointer`, `how_selected`, `retention_type`, `votes_yes`, `votes_no` — all `EXACT`/`FUZZY`(title). Covers judicial appointments, retention elections, congressional terms, committee membership.

### Organization — courts, agencies, committees, firms, party-entities
`org_type` (court, agency, committee, firm, party_entity), `name`, `short_name`, `jurisdiction`, `parent_org_id` — `EXACT`/`FUZZY`(name).

### Citation — edge between two Documents
`citing_document_id`, `cited_document_id`, `citation_string` (`EXACT` — matched even when `cited_document_id` can't be resolved), `treatment` (followed/distinguished/overruled/cited), `depth`.

### Event — docket entries, votes, hearings, retention events, filings
`event_type`, `proceeding_id`, `person_id`, `date` (`EXACT`), `description`/`outcome` (`FUZZY`, `SEM`), `actor`.

### FinancialDisclosure + nested sub-entities
`FinancialDisclosure{person_id, year, filepath, is_amended, investments[], gifts[], debts[], agreements[], reimbursements[], non_investment_income[], spouse_income[]}` — each sub-entity (`Investment`, `Gift`, `Debt`, `Agreement`, `Reimbursement`, `NonInvestmentIncome`, `SpouseIncome`) carries `financial_disclosure_id` and mirrors its CourtListener `disclosures_*` source table field-for-field (see §4). `filepath` is a scan reference — `VISUAL` predicate kind, same as any `ImageAsset`.

### CriminalRecord — attached to Person
`person_id`, `name`, `disposition`, `status`.

### Legislation set (convenience aliases over the entities above — no new types)
| Alias | Backed by |
|---|---|
| Bill | `Proceeding(proceeding_type=bill)` + `Document(doc_type=bill)` per text version |
| Amendment | `Document(doc_type=amendment, proceeding_id=<bill's proceeding>)` |
| Vote | `Event(event_type=vote)` |
| Member | `Person(role_types=[legislator])` |
| Committee | `Organization(org_type=committee)` |
| CommitteeReport | `Document(doc_type=committee_report)` |

### Out of scope (derived/internal, not source data)
`visualizations_scotusmap`, `visualizations_referer`, `visualizations_jsonversion`, `visualizations_scotusmap_clusters`, `auth_user`, `recap_fjcintegrateddatabase` — analytics artifacts and app-internal tables, not legal records to normalize.

---

## 4. Per-source mapping

Badges: 🔵 CourtListener · 🟢 eCFR · 🟣 GovInfo · 🔴 congress.gov · 🟠 Oyez

### 🔵 CourtListener — full ERD coverage

| Source table | Canonical entity |
|---|---|
| `search_court` | `Organization(org_type=court)` |
| `search_docket`, `search_docket_panel` | `Proceeding(proceeding_type=case)` |
| `search_originatingcourtinformation` | fields folded into `Proceeding` (lower-court history) |
| `search_opinioncluster`, `search_opinioncluster_panel`, `search_opinioncluster_non_participating_judges` | `Document(doc_type=opinion)` (cluster = the citable unit) |
| `search_opinion`, `search_opinion_joined_by` | text/author detail folded into `Document.media.text` + `Document` author link |
| `search_opinionscited`, `search_citation` | `Citation` |
| `search_docketentry` | `Event(event_type=docket_entry)` |
| `search_recapdocument` | `ImageAsset`/`source_pdf_url` on `Document` (scanned filings) |
| `people_db_person`, `people_db_person_race`, `people_db_race`, `people_db_politicalaffiliation` | `Person` |
| `people_db_school`, `people_db_education` | folded into `Person` (not separately modeled — low query value) |
| `people_db_source`, `people_db_abarating` | folded into `Person` provenance/notes |
| `people_db_position`, `people_db_retentionevent` | `Position` |
| `people_db_party`, `people_db_party_type`, `people_db_role` | `Person(role_types=[party])` + `Position` |
| `people_db_attorney`, `people_db_attorneyorganization`, `people_db_attorneyorganizationassociation` | `Person(role_types=[attorney])` + `Organization(org_type=firm)` |
| `people_db_criminalcount`, `people_db_criminalcomplaint` | `CriminalRecord` |
| `disclosures_financialdisclosure` | `FinancialDisclosure` |
| `disclosures_investment/gift/debt/agreement/reimbursement/noninvestmentincome/spouseincome/position` | matching sub-entities under `FinancialDisclosure` |

**Live-verified 2026-08-15:** `/api/rest/v4/search/?type=o`, `/courts/`, `/people/` work anonymously. `/dockets/`, `/opinions/` (detail endpoints) return 401 without `COURTLISTENER_API_TOKEN`. See [`dataform/sources/courtlistener.py`](../dataform/sources/courtlistener.py).

### 🟢 eCFR

| Source endpoint | Canonical entity |
|---|---|
| `/api/versioner/v1/titles.json` | `Document.hierarchy_path` root (Title) |
| `/api/versioner/v1/structure/{date}/title-{n}.json` | walked recursively → `Document(doc_type=cfr_section)` per leaf, `hierarchy_path` = joined `label_level` chain |
| `/api/admin/v1/agencies.json` (`cfr_references[]` links agency → title/chapter) | `Organization(org_type=agency)`, linked to `Document.issuing_body_id` via `cfr_references` |
| `/api/versioner/v1/full/{date}/title-{n}.xml` | section body text → `Document.media.text` (XML parsing not yet wired — see open questions) |

**Live-verified 2026-08-15**, no key required. See [`dataform/sources/ecfr.py`](../dataform/sources/ecfr.py).

### 🟣 GovInfo

| Source endpoint | Canonical entity |
|---|---|
| `/collections` | list of `collectionCode`s (BILLS, PLAW, CFR, FR, CRPT, USCOURTS, …) |
| `/collections/{collection}/{startDate}` | packages changed since a date |
| `/packages/{packageId}/summary` | `Document`, `doc_type` derived from `collectionCode` (see `_COLLECTION_TO_DOCTYPE` in the loader); `download.pdfLink` → `source_pdf_url` |
| `/packages/{packageId}/granules` | sub-documents (e.g. individual bill sections) — not yet wired, same pattern as `summary` |

GovInfo re-publishes content also reachable via eCFR/congress.gov/CourtListener — its role here is mainly an additional `source_system` + scan/PDF source, cross-linked via `external_ids["govinfo_package_id"]`, not a new entity family.

**Live-verified end-to-end with a real `GOVINFO_API_KEY` on 2026-08-16.** One correction from the design-time guess: `/collections/{collection}/{startDate}` returns HTTP 400 without an `offsetMark=*` query parameter (undocumented on the JS-rendered docs page, only surfaced by the live 400 body) — fixed in `dataform/sources/govinfo.py`. `packages/{id}/summary` field names (`title`, `dateIssued`, `collectionCode`, `packageId`, `download.pdfLink`, `detailsLink`) matched the design-time guess exactly. See [`dataform/sources/govinfo.py`](../dataform/sources/govinfo.py).

### 🔴 congress.gov

| Source endpoint | Canonical entity |
|---|---|
| `/v3/bill/{congress}`, `/v3/bill/{congress}/{type}/{number}` | `Proceeding(proceeding_type=bill)` + `Document(doc_type=bill)`; `latestAction.text` → `status` |
| `/v3/member` | `Person(role_types=[legislator])`; `partyName` → `political_affiliations` |
| `/v3/committee/{congress}` | `Organization(org_type=committee)` |
| (not yet wired) `/v3/amendment`, `/v3/vote`, `/v3/treaty`, `/v3/nomination` | `Document(doc_type=amendment)`, `Event(event_type=vote)`, same pattern |

**Live-verified end-to-end with a real `CONGRESS_API_KEY` on 2026-08-16** — field names above matched the design-time guess exactly, no corrections needed. See [`dataform/sources/congress.py`](../dataform/sources/congress.py).

### 🟠 Oyez — the audio-native source

| Source endpoint | Canonical entity |
|---|---|
| `/cases?filter=term:{year}` | list of case stubs |
| `/cases/{term}/{docket_number}` | `Proceeding(proceeding_type=case)`; `heard_by[0]` → `Organization(org_type=court)`; `heard_by[0].members[]` → `Person(role_types=[judge])`; `advocates[].advocate` → `Person(role_types=[attorney])` |
| `/case_media/oral_argument_audio/{id}` | `Document(doc_type=oral_argument)`; `media_file[]` (mime=audio/mpeg) → `AudioAsset.audio_ref`; `transcript.sections[].turns[]` → `AudioAsset.timestamp_index` (**already time-aligned to speaker** — no ASR needed for this source) |

Each `timestamp_index` entry: `{start_seconds, end_seconds, speaker, text}`, built directly from `turn.start/stop/speaker.name` and the concatenation of `turn.text_blocks[].text`.

Oyez has no formal API docs page and no id shared with CourtListener — justices/advocates are cross-linked to `people_db_person` by name via `external_ids`, not by a shared source id. **Live-verified 2026-08-15**, no key required. See [`dataform/sources/oyez.py`](../dataform/sources/oyez.py).

---

## 5. Worked examples

**CourtListener opinion** (abridged, real fields from a live `/search/` hit for *Graham v. Connor*):
```json
{
  "envelope": {
    "source_system": "courtlistener",
    "source_id": "9083418",
    "external_ids": {"courtlistener_cluster_id": "9083418", "courtlistener_opinion_id": "9077463"}
  },
  "doc_type": "opinion",
  "title": "Graham v. Connor",
  "citation": "488 U.S. 1001",
  "parallel_citations": ["109 S. Ct. 778"],
  "jurisdiction": "scotus",
  "date_issued": "1989-01-09",
  "media": {"text": {"plain_text": "C. A. 4th Cir. [Certiorari granted, ante, p. 816.] ..."}},
  "proceeding_id": "<uuid of the docket Proceeding>"
}
```

**eCFR section**:
```json
{
  "envelope": {"source_system": "ecfr", "source_id": "title-26-section-1.401",
               "external_ids": {"ecfr_title": "26", "ecfr_identifier": "1.401"}},
  "doc_type": "cfr_section",
  "citation": "26 CFR 1.401",
  "jurisdiction": "federal",
  "status": "published",
  "hierarchy_path": "Title 26—Internal Revenue > Chapter I > Subchapter A—Income Tax > ... > §1.401"
}
```

**Oyez oral argument** (audio + synced transcript, *NY State Rifle & Pistol Assn. v. City of New York*):
```json
{
  "envelope": {"source_system": "oyez", "source_id": "25089",
               "external_ids": {"oyez_audio_id": "25089"}},
  "doc_type": "oral_argument",
  "title": "Oral Argument - December 02, 2019",
  "proceeding_id": "<uuid of the case Proceeding>",
  "media": {"audio": [{
    "audio_ref": "https://s3.amazonaws.com/oyez.case-media.mp3/case_data/2019/18-280/18-280_20191202-argument.delivery.mp3",
    "timestamp_index": [
      {"start_seconds": 9, "end_seconds": 131.36, "speaker": "Paul D. Clement",
       "text": "Mr. Chief Justice, and may it please the Court: Text, history, and tradition all make clear that New York City's restrictive premises license and accompanying transport ban are unconstitutional."}
    ]
  }]}
}
```

---

## 6. Open questions for the team

- **XML/text extraction for eCFR and GovInfo** isn't wired yet — the loaders capture structural metadata (`hierarchy_path`, citation, dates) but not parsed section/bill body text. Needs an XML→`TextAsset` step per source.
- **GovInfo vs. eCFR/congress.gov dedup** — should ingestion resolve `external_ids` at write time (merge into one `Document`), or land all three and merge later in BQL? Leaning toward the latter (simpler ingest, `external_ids` join at query time) but confirm before scaling up.
- **All five sources are now live-verified end-to-end** with real credentials (2026-08-16) — GovInfo and congress.gov are no longer `[VERIFY]`. GovInfo needed one fix (`offsetMark=*` required on the collections-list endpoint); congress.gov needed none.
- **Full CourtListener disclosure/party endpoints** — `models.py` and the ERD mapping above cover them, but `sources/courtlistener.py` only wires up opinions/dockets/people so far. Same `get_json()` pattern extends to `/financial-disclosures/`, `/parties/`, `/attorneys/`.
- **eCFR/GovInfo/congress.gov audio** — none exists; Oyez is the only audio-native source in scope. Confirm that's the intended audio coverage for the multimodal demo.
