"""Synthetic-file diagnostics do not assume Windows metadata equals POSIX metadata."""
import json
import unittest
from snapshot_metadata_probe import diagnose, FIELDS


class MetadataProbeTests(unittest.TestCase):
    def test_probe_reports_only_labels_and_comparison_booleans(self):
        report = diagnose()
        self.assertTrue(report['diagnostic_only'])
        self.assertTrue(report['content_unchanged'])
        for label in ('path_vs_descriptor','descriptor_stable','path_stable'):
            self.assertEqual(set(report[label]), set(FIELDS))
            self.assertTrue(all(type(v) is bool for v in report[label].values()))
        self.assertEqual(set(report), {'diagnostic_only','platform','path_vs_descriptor','descriptor_stable','path_stable','content_unchanged'})
        self.assertNotIn('synthetic-metadata.bin', json.dumps(report))
