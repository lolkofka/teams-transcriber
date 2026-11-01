import shutil
import subprocess
import unittest
from pathlib import Path


class AudioTapScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the injected JS fixture')
    def test_only_received_audio_enters_both_native_bridges(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('audio_tap_fixture.js'))],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
