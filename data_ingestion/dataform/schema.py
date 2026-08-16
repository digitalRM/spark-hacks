"""Maps the canonical dataform (models.py) onto query_language's BQL type
system (query_language/type_system.py), so BQL queries can be typechecked
against the entities/fields this ingestion pipeline actually produces.

Scope decisions (confirmed with the dataform author 2026-08-16):

- TimestampType covers BOTH calendar dates/datetimes (Document.date_issued,
  Person.date_of_birth, RecordEnvelope.ingested_at, ...) AND intra-media
  offsets (AudioAsset.timestamp_index segment start/end). Both are just
  points on an ordered timeline, and the typechecker already allows the
  ordering comparisons (</<=/>/>=) on TimestampType -- there is no separate
  DateType.
- A field whose *purpose* is to address modal content (ImageAsset.image_ref,
  AudioAsset.audio_ref, Person.photo_ref, FinancialDisclosure.filepath) is
  typed ImageType/AudioType even though the Python value is a str (a URL/blob
  key) -- this is what makes it eligible for Fuzzy (VISUAL/AUDIO) matching in
  the typechecker, the same way a SQL BLOB column is typed BLOB rather than
  VARCHAR even though it's stored as bytes. A field that just names or
  labels something (jurisdiction, org name, a citation string) stays TextType.
- AudioAsset.timestamp_index stays List[Dict] on the Pydantic side (not yet
  promoted to a typed submodel), but is still given a precise
  SequenceType(ObjectType(...)) shape here matching the actual keys every
  normalizer populates (start_seconds/end_seconds/speaker/text) -- the BQL
  schema can describe a shape more precisely than the Python type currently
  enforces; nothing about the typechecker requires the two to be the same
  class.
"""
from query_language.type_system import (
    ArrayType,
    AudioType,
    BoolType,
    FloatType,
    FrozenDict,
    ImageType,
    IntType,
    ObjectType,
    OptionalType,
    Schema,
    SequenceType,
    TextType,
    TimestampType,
)


def obj(fields: dict) -> ObjectType:
    return FrozenDict.of(fields)


# ---------------------------------------------------------------------------
# Shared sub-objects (MediaBundle and its assets, embedded inside Document)
# ---------------------------------------------------------------------------

MEDIA_LOCATION: ObjectType = obj({
    "name": OptionalType(TextType()),
    "city": OptionalType(TextType()),
    "region": OptionalType(TextType()),
    "latitude": OptionalType(FloatType()),
    "longitude": OptionalType(FloatType()),
})

# one entry of AudioAsset.timestamp_index -- a transcript turn synced to the
# underlying mp3, which is what makes the audio individually seekable/
# replayable rather than just one opaque blob per Document
TRANSCRIPT_SEGMENT: ObjectType = obj({
    "start_seconds": OptionalType(FloatType()),
    "end_seconds": OptionalType(FloatType()),
    "speaker": OptionalType(TextType()),
    "text": TextType(),
})

TEXT_ASSET: ObjectType = obj({
    "plain_text": OptionalType(TextType()),
    "html": OptionalType(TextType()),
    "language": TextType(),
})

IMAGE_ASSET: ObjectType = obj({
    "page_number": OptionalType(IntType()),
    "image_ref": ImageType(),
    "ocr_text": OptionalType(TextType()),
    "caption": OptionalType(TextType()),
    "captured_at": OptionalType(TimestampType()),
    "location": OptionalType(MEDIA_LOCATION),
})

AUDIO_ASSET: ObjectType = obj({
    "audio_ref": AudioType(),
    "transcript": OptionalType(TextType()),
    "duration_seconds": OptionalType(FloatType()),
    "timestamp_index": SequenceType(TRANSCRIPT_SEGMENT),
    "captured_at": OptionalType(TimestampType()),
    "location": OptionalType(MEDIA_LOCATION),
})

MEDIA_BUNDLE: ObjectType = obj({
    "text": OptionalType(TEXT_ASSET),
    "images": ArrayType(IMAGE_ASSET),
    "audio": ArrayType(AUDIO_ASSET),
})

# every canonical entity embeds RecordEnvelope -- merged into each table below
ENVELOPE_FIELDS = {
    "id": TextType(),
    "source_system": TextType(),
    "source_id": TextType(),
    "source_url": OptionalType(TextType()),
    "ingested_at": TimestampType(),
    "effective_date": OptionalType(TimestampType()),
    "version": OptionalType(TextType()),
}

# ---------------------------------------------------------------------------
# Per-entity tables
# ---------------------------------------------------------------------------

DOCUMENT: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "doc_type": TextType(),
    "title": OptionalType(TextType()),
    "citation": OptionalType(TextType()),
    "parallel_citations": ArrayType(TextType()),
    "jurisdiction": OptionalType(TextType()),
    "issuing_body_id": OptionalType(TextType()),
    "date_issued": OptionalType(TimestampType()),
    "status": OptionalType(TextType()),
    "summary": OptionalType(TextType()),
    "media": MEDIA_BUNDLE,
    "source_pdf_url": OptionalType(TextType()),
    "hierarchy_path": OptionalType(TextType()),
    "proceeding_id": OptionalType(TextType()),
    "cites": ArrayType(TextType()),
    "amends": ArrayType(TextType()),
    "supersedes": OptionalType(TextType()),
})

PROCEEDING: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "proceeding_type": TextType(),
    "number": TextType(),
    "title": OptionalType(TextType()),
    "organization_id": OptionalType(TextType()),
    "party_ids": ArrayType(TextType()),
    "date_filed": OptionalType(TimestampType()),
    "date_terminated": OptionalType(TimestampType()),
    "nature_of_suit": OptionalType(TextType()),
    "status": OptionalType(TextType()),
    "document_ids": ArrayType(TextType()),
})

PERSON: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "role_types": ArrayType(TextType()),
    "name_first": OptionalType(TextType()),
    "name_middle": OptionalType(TextType()),
    "name_last": OptionalType(TextType()),
    "name_suffix": OptionalType(TextType()),
    "date_of_birth": OptionalType(TimestampType()),
    "date_of_death": OptionalType(TimestampType()),
    "gender": OptionalType(TextType()),
    "political_affiliations": ArrayType(TextType()),
    "photo_ref": OptionalType(ImageType()),
})

ORGANIZATION: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "org_type": TextType(),
    "name": TextType(),
    "short_name": OptionalType(TextType()),
    "jurisdiction": OptionalType(TextType()),
    "parent_org_id": OptionalType(TextType()),
    "url": OptionalType(TextType()),
})

POSITION: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "person_id": TextType(),
    "organization_id": TextType(),
    "title": TextType(),
    "date_start": OptionalType(TimestampType()),
    "date_end": OptionalType(TimestampType()),
    "appointer": OptionalType(TextType()),
    "how_selected": OptionalType(TextType()),
    "retention_type": OptionalType(TextType()),
    "votes_yes": OptionalType(IntType()),
    "votes_no": OptionalType(IntType()),
})

CITATION: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "citing_document_id": TextType(),
    "cited_document_id": OptionalType(TextType()),
    "citation_string": TextType(),
    "treatment": OptionalType(TextType()),
    "depth": OptionalType(IntType()),
})

EVENT: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "event_type": TextType(),
    "proceeding_id": OptionalType(TextType()),
    "person_id": OptionalType(TextType()),
    "date": OptionalType(TimestampType()),
    "description": OptionalType(TextType()),
    "actor": OptionalType(TextType()),
    "outcome": OptionalType(TextType()),
})

# financial disclosure family -- each keyed off financial_disclosure_id
INVESTMENT: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "financial_disclosure_id": TextType(),
    "description": OptionalType(TextType()),
    "gross_value_code": OptionalType(TextType()),
    "transaction_during_reporting_period": OptionalType(TextType()),
})

GIFT: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "financial_disclosure_id": TextType(),
    "source": OptionalType(TextType()),
    "description": OptionalType(TextType()),
    "value": OptionalType(TextType()),
})

DEBT: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "financial_disclosure_id": TextType(),
    "creditor_name": OptionalType(TextType()),
    "description": OptionalType(TextType()),
    "value_code": OptionalType(TextType()),
})

AGREEMENT: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "financial_disclosure_id": TextType(),
    "date_raw": OptionalType(TextType()),
    "parties_and_terms": OptionalType(TextType()),
})

REIMBURSEMENT: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "financial_disclosure_id": TextType(),
    "source": OptionalType(TextType()),
    "location": OptionalType(TextType()),
    "purpose": OptionalType(TextType()),
    "items_paid_or_provided": OptionalType(TextType()),
})

NON_INVESTMENT_INCOME: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "financial_disclosure_id": TextType(),
    "source": OptionalType(TextType()),
    "income_amount": OptionalType(TextType()),
})

SPOUSE_INCOME: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "financial_disclosure_id": TextType(),
    "source_type": OptionalType(TextType()),
    "date_raw": OptionalType(TextType()),
})

FINANCIAL_DISCLOSURE: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "person_id": TextType(),
    "year": OptionalType(IntType()),
    "filepath": OptionalType(TextType()),  # whole-document PDF link, matches Document.source_pdf_url's typing
    "page_count": OptionalType(IntType()),
    "media": MEDIA_BUNDLE,  # cover-page thumbnail as a real ImageAsset
    "is_amended": BoolType(),
    "investments": ArrayType(INVESTMENT),
    "gifts": ArrayType(GIFT),
    "debts": ArrayType(DEBT),
    "agreements": ArrayType(AGREEMENT),
    "reimbursements": ArrayType(REIMBURSEMENT),
    "non_investment_income": ArrayType(NON_INVESTMENT_INCOME),
    "spouse_income": ArrayType(SPOUSE_INCOME),
})

CRIMINAL_RECORD: ObjectType = obj({
    **ENVELOPE_FIELDS,
    "person_id": TextType(),
    "name": OptionalType(TextType()),
    "disposition": OptionalType(TextType()),
    "status": OptionalType(TextType()),
})

DATAFORM_SCHEMA: Schema = FrozenDict.of({
    "document": DOCUMENT,
    "proceeding": PROCEEDING,
    "person": PERSON,
    "organization": ORGANIZATION,
    "position": POSITION,
    "citation": CITATION,
    "event": EVENT,
    "financial_disclosure": FINANCIAL_DISCLOSURE,
    "investment": INVESTMENT,
    "gift": GIFT,
    "debt": DEBT,
    "agreement": AGREEMENT,
    "reimbursement": REIMBURSEMENT,
    "non_investment_income": NON_INVESTMENT_INCOME,
    "spouse_income": SPOUSE_INCOME,
    "criminal_record": CRIMINAL_RECORD,
})


if __name__ == "__main__":
    # smoke test: every table is a well-formed ObjectType, and real queries
    # against this schema typecheck -- including the two cases that used to
    # fail before query_language's Optional-unwrap + unnest-path fix
    # (see PR https://github.com/digitalRM/spark-hacks/pull/1).
    from query_language.ast import (
        Between, FieldRef, Fuzzy, Like, Query, TableRef, Unnest,
    )
    from query_language.typechecker import typecheck

    print(f"{len(DATAFORM_SCHEMA)} tables:", ", ".join(DATAFORM_SCHEMA.keys()))

    q = Query(
        select=(FieldRef("d", ("title",)), Unnest(FieldRef("d", ("media", "audio")))),
        source=TableRef("document", "d"),
        where=Fuzzy(FieldRef("d", ("media", "text", "plain_text")), "excessive force claim"),
        group_by=(),
        limit=10,
    )
    typecheck(q, DATAFORM_SCHEMA)
    print("fuzzy-over-text + unnest-audio query typechecks OK")

    # previously failed: Between on a nullable TimestampType field
    # (query_language raised "between requires numeric/timestamp field, got
    # OptionalType(...)" because OptionalType wasn't unwrapped first).
    dob_query = Query(
        select=(FieldRef("p", ("name_last",)),),
        source=TableRef("person", "p"),
        where=Between(FieldRef("p", ("date_of_birth",)), "1900-01-01", "1950-01-01"),
        group_by=(),
        limit=None,
    )
    typecheck(dob_query, DATAFORM_SCHEMA)
    print("between on Person.date_of_birth (nullable TimestampType) typechecks OK")

    # previously impossible: filter on a sub-field of an unnested
    # array-of-struct element -- Unnest had no path, and Fuzzy/Like/Between
    # couldn't reach an unnested audio asset's own audio_ref/captured_at.
    # This is the actual "replayability" scenario: find oral-argument audio
    # whose content matches, addressed by the individual asset (audio_ref),
    # not the whole document.
    audio_fuzzy_query = Query(
        select=(FieldRef("d", ("title",)),),
        source=TableRef("document", "d"),
        where=Fuzzy(
            Unnest(FieldRef("d", ("media", "audio")), ("audio_ref",)),
            "oral argument mentioning excessive force",
        ),
        group_by=(),
        limit=10,
    )
    typecheck(audio_fuzzy_query, DATAFORM_SCHEMA)
    print("fuzzy on unnest(media.audio).audio_ref (per-asset, not whole-document) typechecks OK")

    # filter individual transcript segments by a text pattern -- this is what
    # makes an audio asset seekable/"replayable" rather than one opaque blob:
    # each segment in timestamp_index is its own row once unnested.
    segment_query = Query(
        select=(FieldRef("d", ("title",)),),
        source=TableRef("document", "d"),
        where=Like(
            Unnest(FieldRef("d", ("media", "audio")), ("transcript",)),
            "%excessive force%",
        ),
        group_by=(),
        limit=10,
    )
    typecheck(segment_query, DATAFORM_SCHEMA)
    print("like on unnest(media.audio).transcript (nullable TextType, post-unnest) typechecks OK")
