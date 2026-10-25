"""Both legacy and durable paths must enforce source and content hashes."""
import gzip,hashlib,json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from adaptive_method import digest
from learning_acquisition import download,HOST

class Response:
    def __init__(self,url,blob):self.url=url;self.blob=blob;self.headers={'X-Content-SHA256':hashlib.sha256(blob).hexdigest()}
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,*a):return self.blob

class DownloadTests(unittest.TestCase):
    def test_durable_and_legacy_download_validate_exact_archive(self):
        a={'opportunities':[]};a['archive_id']=digest(a);blob=gzip.compress(json.dumps(a).encode())
        for path in ['/cycle-archive/cycle-123','/cycle-archive/cycle-123?archive_object_id='+'a'*64]:
            self.assertEqual(download(path,lambda url,timeout:Response(url,blob)),a)
        for path in ['https://evil.example','/cycle-archive/cycle-123?x=1','/cycle-archive/cycle-123?archive_object_id=bad','/cycle-archive/cycle-123?archive_object_id='+'a'*64+'&x=1']:
            with self.assertRaisesRegex(ValueError,'PATH_INVALID'):download(path,lambda *a,**k:self.fail('network'))
        with self.assertRaisesRegex(ValueError,'REDIRECT'):
            download('/cycle-archive/cycle-123',lambda *a,**k:Response('https://evil.example',blob))
if __name__=='__main__':unittest.main()
