# Handoff: continuing data collection on spark-box

**Why**: the laptop hit a disk-full crisis mid-collection (an 83GB WAL file from
starved auto-checkpointing under concurrent writers — see "WAL growth" below).
spark-box has far more headroom (1.9TB free disk, 121GB RAM, 20 cores vs. the
laptop's 31GB free) and already holds the current, verified `dataform.db`. This
doc is everything needed to resume ingestion *directly on spark-box* rather
than the laptop.

Everything below is checkpoint-resumable and idempotent (`Store.save()` is an
upsert keyed by a deterministic id) — nothing here risks duplicating or
corrupting existing data. Verified live 2026-08-16.

## Current status (read directly from spark-box's `checkpoints` table)

| Source | Checkpoint key | State | Notes |
|---|---|---|---|
| CourtListener bulk courts | `courtlistener:bulk:courts` | **complete** | full file, tiny |
| CourtListener bulk people | `courtlistener:bulk:people` | **complete** | full file, tiny |
| CourtListener bulk financial-disclosures | `courtlistener:bulk:financial-disclosures` | **complete** | 32,336 rows |
| CourtListener bulk opinion-clusters | `courtlistener:bulk:opinion-clusters` | in_progress, **5,364,000 rows done** | resume with `--limit 0` to drive to true completion |
| CourtListener bulk dockets | `courtlistener:bulk:dockets` | in_progress, **14,764,326 rows done** | resume with `--limit 0`; compressed source file is 4.67GB (bz2), no way to know exact remaining row count without streaming further |
| CourtListener API opinions (6 court lanes) | `courtlistener:opinions:{scotus,ca1..ca5}` | early, 20-80 records each | low priority — the bulk CSV path above already covers this volume far faster; only useful if you want the *very latest* opinions not yet in a quarterly snapshot |
| congress.gov bills (119th) | `congress:bills:119` | in_progress, 18,386 saved | has a `next` cursor, more remains |
| congress.gov bills (118th) | `congress:bills:118` | in_progress, 19,315 saved | has a `next` cursor, more remains |
| eCFR sections | `ecfr:sections` | title-index 49 (of ~50) | close to exhausted — one more session should finish it |
| Oyez oral arguments | `oyez:terms` | term-index 51 of 71 (OT2025→OT1975 done) | 20 terms left, OT1974 down to OT1955 |
| GovInfo BILLS/CFR/CRPT/FR/PLAW/USCOURTS | `govinfo:{collection}` | in_progress, 700-1,205 packages each | all have `next` cursors, more remains |

Empty/never-started (out of scope for this handoff unless you want to extend
the schema's usefulness beyond text/audio/PDF): `citation` table, `position`
table, `event` table, and the financial-disclosure line-item tables
(`investment`, `gift`, `debt`, etc.) — no loader currently populates these.

## One-time setup on spark-box

Target layout (mirrors the laptop's `spark/dataform` + `spark/data` sibling
structure, since `Store`'s default db path is computed as
`<dataform package>/../data/dataform.db`):

```
~/amicus-dataform/
  data/dataform.db      <- already there, verified (sha256 19112596b5...c32bfc)
  dataform/              <- copy this over (see below)
```

spark-box already has Python 3.12.3, git, tmux, and nohup — no runtime
installs needed beyond the pip deps.

**1. Copy the ingestion code over** (from the laptop, run locally):
```bash
rsync -av --exclude='.env' --exclude='__pycache__' \
  /Users/edwinan/Desktop/spark/dataform/ \
  spark-box:~/amicus-dataform/dataform/
```

**2. Copy real API keys over** (separately — never via git, this is a direct
machine-to-machine transfer of secrets you already have locally):
```bash
scp /Users/edwinan/Desktop/spark/dataform/.env spark-box:~/amicus-dataform/dataform/.env
```
Keys used: `COURTLISTENER_API_TOKEN` (optional — raises rate limits, not
required for the bulk CSV path), `GOVINFO_API_KEY` and `CONGRESS_API_KEY`
(required for those two sources respectively).

**3. Install dependencies on spark-box:**
```bash
ssh spark-box
cd ~/amicus-dataform
python3 -m venv .venv
source .venv/bin/activate
pip install -r dataform/requirements.txt
```

## Critical: avoid repeating the WAL-growth crisis

The laptop crisis root cause: many concurrent long-lived writer transactions
(10 `parallel_load.py` lanes + bulk loaders at once) starved SQLite's
automatic WAL checkpointing, and the `-wal` file grew to 83GB before the disk
filled. spark-box's extra headroom buys time, not immunity — the same
concurrency will still grow the WAL unboundedly if left unchecked.

**Set up a periodic checkpoint** before starting any long run, in its own
tmux window:
```bash
while true; do
  sqlite3 ~/amicus-dataform/data/dataform.db "PRAGMA wal_checkpoint(PASSIVE);"
  sleep 300
done
```
`PASSIVE` never blocks writers (unlike `TRUNCATE`/`RESTART`), so it's safe to
run continuously alongside active loaders. Also worth watching disk headroom
directly (`df -h ~` on spark-box) during the first long session to confirm
the checkpoint loop is actually keeping the WAL bounded.

## Resuming each source

Run each in its own **tmux window** (`tmux new -s ingest`, then `Ctrl-b c` per
window) so sessions survive SSH disconnects — you will not want to keep a
laptop SSH session open for a multi-hour run.

**CourtListener bulk (dockets + opinion-clusters) — highest remaining volume:**
```bash
cd ~/amicus-dataform && source .venv/bin/activate
python -m dataform.bulk_load_courtlistener --limit 0
```
`--limit 0` means unbounded — it will keep streaming both CSVs from their
persisted row offsets until each is genuinely exhausted (checkpoint flips to
`complete`). This is the single highest-value thing to resume; it dwarfs
everything else in row count.

**GovInfo (6 collections, session-bounded by design):**
```bash
cd ~/amicus-dataform && source .venv/bin/activate
python -m dataform.load_govinfo --per-collection 500
```
Re-run this repeatedly (or loop it) — each invocation processes up to 500
packages per collection then checkpoints and exits. Raise `--per-collection`
now that spark-box isn't disk/time-constrained the way the laptop was.

**Everything else (courtlistener API lanes, congress bills, eCFR, Oyez)** —
all live in the 10-lane parallel driver:
```bash
cd ~/amicus-dataform && source .venv/bin/activate
python -m dataform.parallel_load --duration 7200 --only ecfr,oyez,congress
```
`--duration` is in seconds; omit `--only` to also re-run the low-priority
CourtListener API lanes. Safe to re-invoke repeatedly — every lane resumes
from its own persisted checkpoint automatically (`--resume` is a no-op flag,
resuming is always the default).

**Do not run `--reset`** on any of these — it wipes all records and
checkpoints.

## Monitoring progress

Checkpoints live in the db itself, so progress is always inspectable without
touching a log file:
```bash
sqlite3 ~/amicus-dataform/data/dataform.db \
  "SELECT key, records_done, updated_at FROM checkpoints ORDER BY key;"
```
Entity counts:
```bash
sqlite3 ~/amicus-dataform/data/dataform.db \
  "SELECT 'document', COUNT(*) FROM document
   UNION ALL SELECT 'proceeding', COUNT(*) FROM proceeding
   UNION ALL SELECT 'person', COUNT(*) FROM person
   UNION ALL SELECT 'financialdisclosure', COUNT(*) FROM financialdisclosure;"
```

## Getting the updated db back to the laptop (when needed)

The same delta-transfer approach used to get the db onto spark-box works in
reverse — rsync only needs to send the bytes that actually changed:
```bash
sqlite3 ~/amicus-dataform/data/dataform.db "PRAGMA wal_checkpoint(TRUNCATE);"  # merge WAL first
rsync -av spark-box:~/amicus-dataform/data/dataform.db /Users/edwinan/Desktop/spark/data/dataform.db
```
Verify with `sha256sum` on both ends before trusting the copy, same as done
for this handoff's own transfer.
