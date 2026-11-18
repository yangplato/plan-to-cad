"""Regression for late rejection of mixed blind observations and stale second submissions."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import ezdxf
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from blind_observations import normalize
from check_cad_logic import freeze_review, inspect, review_gaps
from freeze_source_inventory import seal, verified_inventory
from native_environment import probe
from native_readback import digest
from repair_batch import provenance, seal_submission, overturned, unchanged_reference_layer
from review_working_plan import model_findings
from test_release_geometry_gate import fixture


class WorkflowTests(unittest.TestCase):
    def test_inventory_disagreement_stays_open_separately_from_a_measured_dwg_error(self):
        from validation_v16 import source_findings
        failed,pending=source_findings(dict(status="different",failures=["check/A ↔ B","native/wall_tones"],
                                           pending=["native/extra/14E"]))
        self.assertEqual(failed,{"双份原图清单/native/wall_tones"})
        self.assertEqual(pending,{"双份原图清单/读图分歧/check/A ↔ B","双份原图清单/native/extra/14E"})

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        native = self.root/"review/native"
        native.mkdir(parents=True)
        self.doc = ezdxf.new()
        self.dxf = native/"readback.dxf"
        self.doc.saveas(self.dxf)
        (native/"plan.dwg").write_bytes(b"test-identity")
        self.blind = self.root/"review/cad-only-frozen.json"
        self.record = {"schema": "cad-observations/1", "reviewer": "one", "dxf_sha256": digest(self.dxf),
                       "junctions": "corner", "access": "door", "symbols": "hinge", "sheet": "labels",
                       "checks": [{"id": "cabinet", "kind": "confirmed", "location": "east column",
                                   "evidence": "cabinet overlaps column"},
                                  {"id": "return", "kind": "needs_source", "location": "south wall",
                                   "evidence": "check return against plan"}]}

    def write(self, path, value):
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_deferred_items_never_join_repair_list(self):
        result = inspect(self.doc)
        freeze_review(self.blind, self.dxf, result, self.record, require_form=True)
        frozen = json.loads(self.blind.read_text())
        self.assertEqual(frozen["open_defects"], ["cabinet"])
        self.assertIn("return", review_gaps(result, frozen, digest(self.dxf)))
        with self.assertRaises(ValueError):
            normalize(dict(self.record, open_defects=["return", "cabinet"]), result)
        with self.assertRaises(ValueError):
            normalize(self.record, {"findings": [{"id": "screen"}]})

    def test_source_list_is_sealed_after_blind_and_bound_to_original(self):
        orig = "a"*64
        self.write(self.root/"registration.json", {"source_sha256": orig})
        self.write(self.root/"ledger.json", {"source_sha256": orig, "drafting_version": "1.6.0",
                                              "repair_batch": {"stage": "initial"}})
        self.write(self.root/"source-inputs/author.json", {"source_sha256": orig, "session_id": "drawing"})
        other = self.root/"second.json"
        self.write(other, {"source_sha256": orig, "session_id": "independent", "source_only": True})
        with self.assertRaises(ValueError):
            seal(self.root, other)
        freeze_review(self.blind, self.dxf, inspect(self.doc), self.record, require_form=True)
        seal(self.root, other)
        self.assertTrue(verified_inventory(self.root).is_file())
        with self.assertRaises(FileExistsError):
            seal(self.root, other)
        assessment = {"reviewer": "peer", "dwg_sha256": digest(self.dxf.with_name("plan.dwg")),
                      "issues": [{"id": "cabinet", "kind": "repair"}]}
        self.assertEqual(provenance(self.root, assessment, orig)["stage"], "revised")
        self.write(self.blind, {})
        with self.assertRaises(ValueError):
            verified_inventory(self.root)

    def test_only_same_submitted_version_can_repeat(self):
        seal_submission(self.root, "r01", "r00", {"issues": []})
        seal_submission(self.root, "r01", "r00", {"issues": []})
        with self.assertRaisesRegex(ValueError, "已用完"):
            seal_submission(self.root, "r02", "r00", {"issues": []})

    def test_a_blind_reference_layer_misclassification_needs_reproducible_native_evidence(self):
        native=self.dxf.parent
        photo=native/"photo.png";photo.write_bytes(b"synthetic file hash")
        layer=self.doc.layers.new("A-XREF")
        definition=self.doc.add_image_def(filename=photo.name,size_in_pixel=(10,10))
        definition.dxf.loaded=1
        image=self.doc.modelspace().add_image(definition,(0,0),(10,10),dxfattribs={"layer":layer.dxf.name})
        layer.off();layer.lock();layer.dxf.plot=0
        self.doc.saveas(self.dxf)
        self.write(self.root/"ledger.json",{"source_reference":dict(layer=layer.dxf.name,filename=photo.name,
                   handle=image.dxf.handle,source_sha256=digest(photo))})
        self.write(native/"capture.json",{"files":{k:digest(native/k) for k in ("plan.dwg","readback.dxf")}})
        self.write(self.blind,{"dxf_sha256":digest(self.dxf),"checks":[dict(id="ref",kind="confirmed",
                   subject="source_reference_layer_defaults",location="layer A-XREF",evidence="read in CAD")]})
        proof=dict(check="source_reference_layer_defaults",layer=layer.dxf.name,initial_dxf_sha256=digest(self.dxf))
        issue=dict(id="ref",kind="inference",native_reclassification=proof)
        self.assertEqual(overturned(self.root,{"issues":[issue]}),{"ref"})
        self.assertFalse(unchanged_reference_layer(self.root,self.root,"cabinet",proof))
        from shutil import copytree
        candidate=self.root/"R01"
        copytree(self.root/"review",candidate/"review")
        (candidate/"ledger.json").write_bytes((self.root/"ledger.json").read_bytes())
        layer.on();self.doc.saveas(candidate/"review/native/readback.dxf")
        revised=candidate/"review/native"
        self.write(revised/"capture.json",{"files":{k:digest(revised/k) for k in ("plan.dwg","readback.dxf")}})
        self.assertFalse(unchanged_reference_layer(self.root,candidate,"ref",proof))

    def test_missing_console_has_no_edit_claim(self):
        result = probe(self.root/"preflight", self.root/"unavailable")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["stage"], "locate_console")
        self.assertNotIn("dimension_edit", result)

    def test_different_printed_wall_tones_block_release(self):
        case = self.root/"tone"
        case.mkdir()
        assessment = fixture(case)
        path = case/"author-summary.json"
        original = json.loads(path.read_text())
        self.write(path, dict(original, wall_colour_stations="different"))
        hard, _ = model_findings(case, assessment["dwg_sha256"])
        self.assertIn("墙芯深浅分界/成品与原图不符", hard)
        self.write(path, dict(original, wall_colour_stations="review"))
        hard, pending = model_findings(case, assessment["dwg_sha256"])
        self.assertIn("墙芯深浅分界/待核", pending)
        self.assertNotIn("墙芯深浅分界/成品与原图不符", hard)
