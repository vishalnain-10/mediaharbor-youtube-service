import importlib.util
import json
import os
import re
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

scratch=tempfile.TemporaryDirectory(prefix='mediaharbor-test-')
os.environ['DATA_DIR']=scratch.name
os.environ['MEDIAHARBOR_BACKEND_TOKEN']='unit-test-token-not-a-real-credential-12345'
spec=importlib.util.spec_from_file_location('service',Path(__file__).with_name('youtube_server.py'))
service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)

class BackendTests(unittest.TestCase):
    def test_url_boundary(self):
        for url in ['https://www.youtube.com/watch?v=ak-Ei831dtk','https://youtu.be/ak-Ei831dtk','https://m.youtube.com/shorts/ak-Ei831dtk']:
            self.assertEqual(service.video_id(url),'ak-Ei831dtk')
        for url in ['https://youtube.com.evil.com/watch?v=ak-Ei831dtk','http://127.0.0.1/','https://user:pass@youtube.com/watch?v=ak-Ei831dtk','https://youtube.com/watch?v=bad']:
            with self.assertRaises(ValueError):service.video_id(url)
    def test_quality_cannot_exceed_available_source(self):
        with patch.object(service,'info',return_value={'qualities':[1080,720], 'title':'test','videoId':'ak-Ei831dtk'}):
            with self.assertRaises(ValueError):service.create_job('https://youtu.be/ak-Ei831dtk','2160')
    def test_sizes_use_selected_tracks_not_stale_downloads(self):
        class FakeYDL:
            def __init__(self, options): self.options = options
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def process_ie_result(self, data, download=False):
                cap = int(re.search(r'height<=(\d+)', self.options['format'])[1])
                return {'requested_formats': [{'filesize': cap * 1000}, {'filesize': 100}], 'requested_downloads': [{'filesize': 999}]}
        with patch.dict('sys.modules', {'yt_dlp': types.SimpleNamespace(YoutubeDL=FakeYDL)}):
            self.assertEqual(service.quality_sizes({'duration':60}, [2160,360]), {'2160':2160100,'360':360100})
    def test_stream_size(self):
        self.assertEqual(service.stream_bytes({'filesize': 1200, 'tbr': 100}, 60), 1200)
        self.assertEqual(service.stream_bytes({'filesize_approx': 2300}, 60), 2300)
        self.assertEqual(service.stream_bytes({'tbr': 100}, 60), 750000)
        self.assertIsNone(service.stream_bytes({}, 60))
    def test_audio_validation_and_queue(self):
        with self.assertRaises(ValueError):service.create_job('https://youtu.be/ak-Ei831dtk','original','audio','flac',192)
        with patch.object(service,'info',return_value={'qualities':[1080], 'title':'test','videoId':'ak-Ei831dtk'}), patch.object(service.POOL,'submit') as submit:
            try:
                queued=service.create_job('https://youtu.be/ak-Ei831dtk','original','audio','mp3',192)
                self.assertEqual(queued['mode'],'audio');self.assertEqual(queued['extension'],'mp3');self.assertIsNone(queued['height'])
                self.assertEqual(submit.call_args.args[-3:],('audio','mp3',192))
            finally:service.SLOTS.release()
    def test_audio_size_options(self):
        class FakeYDL:
            def __init__(self, options): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def process_ie_result(self, data, download=False): return {'filesize':12345}
        with patch.dict('sys.modules', {'yt_dlp':types.SimpleNamespace(YoutubeDL=FakeYDL)}):
            options=service.audio_options({'duration':60})
        self.assertEqual([x['id'] for x in options],['mp3-128','mp3-192','mp3-320','m4a'])
        self.assertEqual(options[1]['estimatedSize'],1440000);self.assertEqual(options[3]['estimatedSize'],12345)
    def test_long_recorded_stream_and_duration_limit(self):
        payload={'title':'Recorded stream','duration':35159,'live_status':'was_live','formats':[{'height':720,'vcodec':'avc1'}]}
        url='https://www.youtube.com/watch?v=irNhWMG560k'
        service.CACHE.clear()
        with patch.object(service.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps(payload))), patch.object(service,'quality_sizes',return_value={}), patch.object(service,'audio_options',return_value=[]):
            self.assertEqual(service.info(url)['duration'],35159)
        service.CACHE.clear();payload['duration']=43201
        with patch.object(service.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps(payload))):
            with self.assertRaisesRegex(ValueError,'twelve hours'):service.info(url)
    def test_progress_check_tolerates_part_file_rename(self):
        transient=types.SimpleNamespace(is_file=lambda:True,stat=lambda:(_ for _ in ()).throw(FileNotFoundError()))
        folder=types.SimpleNamespace(iterdir=lambda:iter([transient]))
        self.assertEqual(service.folder_bytes(folder),0)
    def test_completed_file_reused_and_recovered(self):
        key='b'*32;folder=Path(scratch.name)/key;folder.mkdir(exist_ok=True);file=folder/'video.mp4';file.write_bytes(b'video'*256)
        state={'id':key,'status':'ready','videoId':'HnRNXGKqNIE','mode':'video','height':1080,'extension':'mp4','size':file.stat().st_size,'updated':service.time.time()}
        service.JOBS[key]=state;service.persist_ready(state)
        with patch.object(service,'info',return_value={'qualities':[1080],'videoId':'HnRNXGKqNIE','title':'test'}),patch.object(service.POOL,'submit') as submit:
            self.assertEqual(service.create_job('https://youtu.be/HnRNXGKqNIE','1080')['id'],key);submit.assert_not_called()
        del service.JOBS[key];service.recover_ready_jobs();self.assertEqual(service.JOBS[key]['size'],1280)
    def test_failed_job_resumes_existing_folder(self):
        key='c'*32;(Path(scratch.name)/key).mkdir(exist_ok=True)
        service.JOBS[key]={'id':key,'status':'failed','videoId':'HnRNXGKqNIE','mode':'video','height':720,'extension':'mp4','updated':0,'error':'old error'}
        with patch.object(service,'info',return_value={'qualities':[720],'videoId':'HnRNXGKqNIE','title':'test'}),patch.object(service.POOL,'submit') as submit:
            try:
                result=service.create_job('https://youtu.be/HnRNXGKqNIE','720');self.assertEqual(result['id'],key);self.assertEqual(result['status'],'queued');self.assertIsNone(result['error']);self.assertEqual(submit.call_args.args[1],key)
            finally:service.SLOTS.release();service.JOBS.pop(key,None)
    def test_download_tickets_are_scoped_and_expire(self):
        key='d'*32;folder=Path(scratch.name)/key;folder.mkdir(exist_ok=True);(folder/'video.mp4').write_bytes(b'video'*256)
        state={'id':key,'status':'ready','videoId':'HnRNXGKqNIE','mode':'video','height':1080,'extension':'mp4','size':1280,'updated':service.time.time()};service.JOBS[key]=state
        token=service.download_ticket(state)
        self.assertTrue(service.valid_download_ticket(token,key))
        self.assertFalse(service.valid_download_ticket(token,'e'*32))
        self.assertFalse(service.valid_download_ticket(token+'x',key))
        self.assertFalse(service.valid_download_ticket(service.download_ticket(state,expires=int(service.time.time())-1),key))
        (folder/'video.mp4').write_bytes(b'changed');self.assertFalse(service.valid_download_ticket(token,key))
    def test_cropped_quality_and_oversize_selection(self):
        metadata={'qualities':[2140,1426,1070,714,142],'videoId':'169ZzvPxvvM','title':'cropped','qualitySizes':{'2140':14557089670,'1070':3103664841}}
        with patch.object(service,'info',return_value=metadata),patch.object(service.POOL,'submit') as submit:
            with self.assertRaisesRegex(ValueError,'file-size limit'):service.create_job('https://youtu.be/169ZzvPxvvM','2140')
            submit.assert_not_called()
            try:
                result=service.create_job('https://youtu.be/169ZzvPxvvM','1070');self.assertEqual(result['height'],1070)
            finally:service.SLOTS.release();service.JOBS.pop(result['id'],None)
    def test_private_errors(self):
        self.assertNotIn('googlevideo',service.friendly_error('failed https://googlevideo.com/secret'))
    def test_authentication_and_range(self):
        job_id='a'*32;folder=Path(scratch.name)/job_id;folder.mkdir(exist_ok=True);(folder/'video.mp4').write_bytes(b'0123456789'*1024)
        service.JOBS[job_id]={'id':job_id,'status':'ready','videoId':'ak-Ei831dtk','height':2160,'updated':0}
        server=service.ThreadingHTTPServer(('127.0.0.1',0),service.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with self.assertRaises(HTTPError) as error:urlopen(base+'/info?url=x')
            self.assertEqual(error.exception.code,401)
            issue=Request(base+'/jobs/'+job_id+'/ticket',data=b'{}',headers={'Authorization':'Bearer '+service.TOKEN,'Content-Type':'application/json'})
            with urlopen(issue) as response:ticket=json.load(response)['ticket']
            signed=base+'/jobs/'+job_id+'/file?ticket='+ticket
            with urlopen(Request(signed,headers={'Range':'bytes=0-9'})) as response:self.assertEqual(response.status,206);self.assertEqual(response.read(),b'0123456789')
            with self.assertRaises(HTTPError) as denied:urlopen(base+'/jobs/'+job_id+'?ticket='+ticket)
            self.assertEqual(denied.exception.code,401);denied.exception.close()
            req=Request(base+'/jobs/'+job_id+'/file',headers={'Authorization':'Bearer '+service.TOKEN,'Range':'bytes=10-19'})
            with urlopen(req) as response:
                self.assertEqual(response.status,206);self.assertEqual(response.read(),b'0123456789');self.assertEqual(response.headers['Content-Range'],'bytes 10-19/10240');etag=response.headers['ETag']
            req=Request(base+'/jobs/'+job_id+'/file',headers={'Authorization':'Bearer '+service.TOKEN,'Range':'bytes=10-19','If-Range':etag})
            with urlopen(req) as response:self.assertEqual(response.status,206);self.assertEqual(response.read(),b'0123456789')
            req=Request(base+'/jobs/'+job_id+'/file',headers={'Authorization':'Bearer '+service.TOKEN,'Range':'bytes=10-19','If-Range':'stale-etag'})
            with urlopen(req) as response:self.assertEqual(response.status,200);self.assertEqual(len(response.read()),10240)
            req=Request(base+'/jobs/'+job_id+'/file',headers={'Authorization':'Bearer '+service.TOKEN,'Range':'bytes=999999-'})
            with self.assertRaises(HTTPError) as error:urlopen(req)
            self.assertEqual(error.exception.code,416)
            service.JOBS[job_id].update(mode='audio',extension='mp3',bitrate=192)
            (folder/'audio.mp3').write_bytes(b'audio-test'*1000)
            req=Request(base+'/jobs/'+job_id+'/file',headers={'Authorization':'Bearer '+service.TOKEN,'Range':'bytes=0-9'})
            with urlopen(req) as response:
                self.assertEqual(response.status,206);self.assertEqual(response.headers['Content-Type'],'audio/mpeg');self.assertIn('192kbps.mp3',response.headers['Content-Disposition']);self.assertEqual(response.read(),b'audio-test')
        finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
