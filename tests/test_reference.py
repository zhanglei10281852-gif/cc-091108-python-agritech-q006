import json
import unittest
from pathlib import Path


class ReferenceDataTest(unittest.TestCase):
    def test_material_quantities_are_conserved(self):
        data = json.loads((Path(__file__).parents[1] / "reference" / "domain.json").read_text(encoding="utf-8"))
        self.assertEqual(data["domain"], "produce-precooling")
        split = data["scan_events"][0]
        source = next(item["quantity"] for item in data["containers"] if item["id"] == split["from"][0])
        self.assertEqual(source, sum(split["quantities"]))
        self.assertTrue(all(rule["target_core_c"] > 0 for rule in data["rules"]))


if __name__ == "__main__":
    unittest.main()
