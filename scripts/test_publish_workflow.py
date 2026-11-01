"""Execute the real publication shell with offline GitHub/Docker boundaries."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

FAKE_GH = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['PUBLISH_TEST_STATE'])
s=json.loads(p.read_text()) if p.exists() else None
a=sys.argv[1:]
if a[:2]==['release','view']:
 if s is None:sys.exit(1)
 print('https://api.github.com/repos/ckeiituk/subscription-page/releases/123')
elif a[0]=='api':
 if not a[1].endswith('/releases/123') or s is None:sys.exit(1)
 print(json.dumps(s))
elif a[:2]==['release','create']:
 s={'draft':True,'target_commitish':os.environ['GITHUB_SHA'],'assets':[]}
elif a[:2]==['release','upload']:
 assert s['draft'],'Published assets must never be overwritten'
 s['assets']=[{'name':n,'size':Path(n).stat().st_size} for n in ('image.tar.gz','image.tar.gz.sha256')]
elif a[:2]==['release','edit']:
 assert len(s['assets'])==2
 s['draft']=False
else:raise RuntimeError('Unexpected gh operation')
if s is not None:p.write_text(json.dumps(s))
'''


class PublicationTests(unittest.TestCase):
    def exercise(self, initial=None):
        workflow = Path(__file__).parents[1] / ".github/workflows/x-out-check.yml"
        step = workflow.read_text().split("      - name: Publish commit image as public release assets", 1)[1]
        shell = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            if initial is not None:
                state.write_text(json.dumps(initial))
            (root / "gh").write_text(FAKE_GH)
            (root / "gh").chmod(0o755)
            (root / "docker").write_text("#!/bin/sh\nprintf 'synthetic image archive'\n")
            (root / "docker").chmod(0o755)
            environment = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                           "PUBLISH_TEST_STATE": str(state), "GITHUB_SHA": "a" * 40,
                           "GITHUB_REPOSITORY": "ckeiituk/subscription-page", "IMAGE": "synthetic"}
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", shell], cwd=root,
                                    env=environment, text=True, capture_output=True)
            final = json.loads(state.read_text()) if state.exists() else None
            return result, final

    def test_new_release_is_verified_via_id_while_still_draft(self):
        result, final = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(final["draft"])
        self.assertEqual({a["name"] for a in final["assets"]}, {"image.tar.gz", "image.tar.gz.sha256"})

    def test_partial_draft_is_repaired_before_publication(self):
        result, final = self.exercise({"draft": True, "target_commitish": "a" * 40,
                                       "assets": [{"name": "image.tar.gz", "size": 5}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(final["draft"])
        self.assertEqual(len(final["assets"]), 2)

    def test_incomplete_published_release_fails_without_overwriting_it(self):
        original = {"draft": False, "target_commitish": "a" * 40,
                    "assets": [{"name": "image.tar.gz", "size": 5}]}
        result, final = self.exercise(original)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(final, original)


if __name__ == "__main__":
    unittest.main()
