"""optimizer.stats and scripts/bootstrap_corpus: structure from the registry, numbers
from a counted database, and the two composed.

    python3 -m unittest discover -s optimizer/tests -t .
"""
from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from optimizer.plan import Provenance
from optimizer.stats import (
    CollectionStats, OptionalStats, ScalarStats, avg_tokens, coverage, fanout,
    fanout_provenance, from_registry, overlay, partition_rows, placeholders, rows,
)
from query_language import schema as registry
from query_language.ast import FieldRef

import contextlib
import importlib.util
import io
import sys
_BOOT = Path(__file__).resolve().parents[2] / 'scripts' / 'bootstrap_corpus.py'
_spec = importlib.util.spec_from_file_location('bootstrap_corpus', _BOOT)
bootstrap = importlib.util.module_from_spec(_spec)                   # scripts/ is not a package
sys.modules['bootstrap_corpus'] = bootstrap
_spec.loader.exec_module(bootstrap)  # type: ignore[union-attr]

def ref(s: str) -> FieldRef:
    t, _, rest = s.partition('.')
    return FieldRef(t, tuple(rest.split('.')))

class TestFromRegistry(unittest.TestCase):
    def test_every_registry_field_gets_a_node_for_every_shipped_schema(self):
        for name in registry.available():
            reg = registry.load(name)
            s = from_registry(reg)
            self.assertEqual(set(s.tables), set(reg.tables))
            for spec in reg.fields.values():
                self.assertIsNotNone(coverage(s, ref(spec.name)), spec.name)
                # nothing hand-written: every number is a placeholder
            self.assertIn(f'{next(iter(reg.tables))} (rows)', list(placeholders(s)))

    def test_shapes_follow_the_registry(self):
        s = from_registry(registry.load('dataform'))
        self.assertEqual(fanout(s, ref('document.media.audio')), 120.0)
        self.assertEqual(fanout(s, ref('proceeding.party_ids')), 4.0)
        self.assertEqual(fanout(s, ref('document.title')), 1.0)
        self.assertTrue(from_registry(registry.load('dataform')).derivations)
        d = s.derivations[ref('financialdisclosure.filepath')]
        self.assertEqual(d.source, ref('financialdisclosure.filepath'))
        cl = from_registry(registry.load('courtlistener'))
        self.assertEqual(cl.derivations[ref('docket.argument')].source, ref('audio.local_path'))
        self.assertEqual(rows(cl, 'cluster'), (1000.0, Provenance.PLACEHOLDER))

class TestOverlay(unittest.TestCase):
    def test_measured_numbers_land_and_flip_provenance(self):
        s = overlay(from_registry(registry.load('dataform')), {'tables': {
            'document': {'rows': 5638827, 'partitions': {'doc_type': {'opinion': 5364751}},
                         'fields': {
                'title': {'kind': 'text', 'coverage': 0.9966, 'avg_tokens': 7.8},
                'media.audio': {'kind': 'array', 'coverage': 0.000828, 'fanout': 1.0,
                                'element': {'timestamp_index': {
                                    'kind': 'array', 'coverage': 0.98, 'fanout': 234.9,
                                    'provenance': 'extrapolated',
                                    'element': {'text': {'kind': 'text', 'coverage': 1.0,
                                                         'avg_tokens': 58.3}}}}},
                'cites': {'kind': 'array', 'coverage': 0.0, 'fanout': 0.0},
                'citation': {'kind': 'null', 'coverage': 0.0},
            }},
            'not_a_registry_table': {'rows': 5, 'fields': {}},
        }})
        self.assertEqual(rows(s, 'document'), (5638827.0, Provenance.MEASURED))
        self.assertEqual(partition_rows(s, 'document', 'doc_type', 'opinion'), 5364751.0)
        self.assertEqual(partition_rows(s, 'document', 'doc_type', 'nope'), 0.0)
        self.assertIsNone(partition_rows(s, 'document', 'source_system', 'x'))
        self.assertAlmostEqual(coverage(s, ref('document.title')), 0.9966)
        self.assertAlmostEqual(avg_tokens(s, ref('document.title')), 7.8)
        # audio: optional (0.08% of documents) around a collection of 1 recording ...
        self.assertAlmostEqual(coverage(s, ref('document.media.audio')), 0.000828)
        self.assertEqual(fanout(s, ref('document.media.audio')), 1.0)
        self.assertIs(fanout_provenance(s, ref('document.media.audio')), Provenance.MEASURED)
        # ... whose element structure goes deeper than the registry
        self.assertAlmostEqual(fanout(s, ref('document.media.audio.timestamp_index')), 234.9)
        self.assertIs(fanout_provenance(s, ref('document.media.audio.timestamp_index')),
                      Provenance.EXTRAPOLATED)
        self.assertAlmostEqual(avg_tokens(s, ref('document.media.audio.timestamp_index.text')), 58.3)
        # never-present fields are measured, not placeholders
        self.assertEqual(coverage(s, ref('document.cites')), 0.0)
        self.assertEqual(fanout(s, ref('document.cites')), 0.0)
        left = [p for p in placeholders(s) if p.startswith('document.c')]
        self.assertEqual(left, [])
        # unmeasured registry fields stay placeholders; unknown tables are ignored
        self.assertIn('document.summary', list(placeholders(s)))
        self.assertNotIn('not_a_registry_table', s.tables)

class TestBootstrapEndToEnd(unittest.TestCase):
    """A tiny dataform-shaped SQLite db, counted by the script, overlaid on the registry."""

    def _mini_db(self, path: Path) -> None:
        rng = random.Random(7)
        c = sqlite3.connect(path)
        for t in ['organization', 'person', 'position', 'proceeding', 'document', 'citation',
                  'event', 'financialdisclosure', 'criminalrecord', 'checkpoints']:
            c.execute(f'CREATE TABLE {t}(id TEXT PRIMARY KEY, source_system TEXT, source_id TEXT,'
                      f' doc_type TEXT, date TEXT, data TEXT NOT NULL)')
            c.execute(f'CREATE INDEX idx_{t}_s ON {t}(source_system, source_id)')
            c.execute(f'CREATE INDEX idx_{t}_d ON {t}(doc_type)')
        for i in range(400):
            audio = i % 100 == 0
            d = {'envelope': {'id': f'd{i}', 'source_system': 'oyez' if audio else 'courtlistener'},
                 'doc_type': 'oral_argument' if audio else 'opinion',
                 'title': 'x' * 40, 'summary': 'y' * 400 if i % 4 == 0 else None,
                 'media': {'text': None, 'images': [],
                           'audio': [{'audio_ref': 'u', 'timestamp_index': [
                               {'start_seconds': 1.0, 'speaker': 'J', 'text': 'w' * 80}
                               for _ in range(rng.randint(100, 300))]}] if audio else []},
                 'cites': []}
            c.execute('INSERT INTO document VALUES (?,?,?,?,?,?)',
                      (f'd{i}', d['envelope']['source_system'], str(i), d['doc_type'], None,
                       json.dumps(d)))
        c.commit(); c.close()

    def test_counts_match_the_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / 'mini.db', Path(tmp) / 'dataform.json'
            self._mini_db(db)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bootstrap.main(['--schema', 'dataform', '--db', str(db), '--out', str(out)])
            self.assertEqual(rc, 0)
            m = json.loads(out.read_text())
            doc = m['tables']['document']
            self.assertEqual(doc['rows'], 400)
            self.assertEqual(doc['partitions']['doc_type'], {'opinion': 396, 'oral_argument': 4})
            self.assertNotIn('date', doc['partitions'])          # all-null column: no partition
            f = doc['fields']
            self.assertEqual(f['title']['coverage'], 1.0)
            self.assertEqual(f['title']['avg_tokens'], 10.0)     # 40 chars / 4
            self.assertEqual(f['summary']['coverage'], 0.25)
            self.assertEqual(f['summary']['avg_tokens'], 100.0)
            self.assertEqual(f['media.audio']['coverage'], 0.01)
            self.assertEqual(f['media.audio']['fanout'], 1.0)
            seg = f['media.audio']['element']['timestamp_index']
            self.assertTrue(100 <= seg['fanout'] <= 300)
            self.assertEqual(seg['element']['text']['avg_tokens'], 20.0)
            self.assertEqual(seg['provenance'], 'measured')      # 4 recordings, all pulled
            self.assertEqual(f['cites']['coverage'], 0.0)
            self.assertEqual(f['media.text.plain_text']['coverage'], 0.0)
            self.assertEqual(m['tables']['citation']['rows'], 0)
            self.assertEqual(m['tables']['citation']['fields']['treatment']['coverage'], 0.0)

            s = overlay(from_registry(registry.load('dataform')), m)
            self.assertEqual(rows(s, 'document'), (400.0, Provenance.MEASURED))
            self.assertAlmostEqual(coverage(s, ref('document.media.audio')), 0.01)
            self.assertAlmostEqual(avg_tokens(s, ref('document.media.audio.timestamp_index.text')), 20.0)
            self.assertEqual([p for p in placeholders(s) if not p.endswith('(derivation)')], [])

    def test_refuses_a_db_that_is_not_the_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'other.db'
            c = sqlite3.connect(db); c.execute('CREATE TABLE cluster(id TEXT)'); c.commit(); c.close()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = bootstrap.main(['--schema', 'dataform', '--db', str(db),
                                     '--out', str(Path(tmp) / 'x.json')])
            self.assertEqual(rc, 1)
            self.assertFalse((Path(tmp) / 'x.json').exists())

if __name__ == '__main__':
    unittest.main()
