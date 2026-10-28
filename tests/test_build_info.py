"""Each result records the Ki-PIDA build that produced it (review 2026-10-02)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class BuildIdentityTests(unittest.TestCase):
    def test_a_result_says_which_build_made_it(self):
        # P02 Rev E's EMC score read 65, 35, 63 and 65 from 2026-09-30 to
        # 2026-10-01 at one board fingerprint; only the tool had changed.
        from analysis_contract import AnalysisResult
        from build_info import build_commit, tool_identity

        with tempfile.TemporaryDirectory() as directory:
            commit = Path(directory, "build_commit.txt")
            commit.write_text("$Format:%h %cs$\n", encoding="utf-8")
            self.assertEqual(build_commit(directory), "")
            commit.write_text("ec69dbf 2026-10-02\n", encoding="utf-8")
            self.assertEqual(build_commit(directory), "ec69dbf 2026-10-02")

        result = AnalysisResult("DC", "DC Power")
        self.assertEqual(result.tool_version, tool_identity())
        self.assertRegex(result.tool_version, r"^\d+\.\d+\.\d+\+[0-9a-f]{8}")
        self.assertEqual(AnalysisResult.from_dict(result.to_dict()).tool_version, result.tool_version)
        saved = result.to_dict()
        del saved["tool_version"]
        # A result saved before builds were recorded is not this build's.
        self.assertEqual(AnalysisResult.from_dict(saved).tool_version, "")


if __name__ == "__main__":
    unittest.main()
