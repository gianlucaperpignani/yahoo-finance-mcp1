"""Run the Apps Script storage/authentication harness in ordinary Python CI."""
import shutil
import subprocess
import unittest
from pathlib import Path


class GatewayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for Apps Script harness')
    def test_gateway_protocol_and_storage(self):
        script=Path(__file__).with_suffix('.cjs')
        result=subprocess.run(['node',str(script)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
