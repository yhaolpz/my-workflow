import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mw',ROOT/'scripts/workflow.py')
mw=importlib.util.module_from_spec(spec);spec.loader.exec_module(mw)

GRAPH={'1':{'class_type':'MockH3','inputs':{'prompt':'original','seconds':1,'seed':7}},'2':{'class_type':'MockSave','inputs':{'video':['1',0]}}}
CATALOG={'MockH3':{'input':{'required':{'prompt':['STRING',{}],'seconds':['INT',{}],'seed':['INT',{}]}},'output':['VIDEO']},'MockSave':{'input':{'required':{'video':['VIDEO',{}]}},'output':[],'output_node':True}}

class Handler(BaseHTTPRequestHandler):
    posts=0;history={};submitted=[];lose_reply=False;blob=b'not-a-real-video';guide_fail=False
    def log_message(self,*args):pass
    def send(self,data,code=200,raw=False):
        body=data if raw else json.dumps(data).encode();self.send_response(code);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if self.path=='/system_stats':self.send({'system':{'python_version':'test'}})
        elif self.path=='/object_info':self.send(CATALOG)
        elif self.path.startswith('/history/'):
            pid=self.path.split('/')[-1];self.send({pid:Handler.history[pid]} if pid in Handler.history else {})
        elif self.path.startswith('/view?'):self.send(Handler.blob,raw=True)
        elif self.path.startswith('/guide/'):
            if Handler.guide_fail:self.send({},503)
            else:self.send(('official '+self.path).encode(),raw=True)
        else:self.send({},404)
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length',0)))
        if self.path=='/prompt':
            Handler.posts+=1;Handler.submitted.append(json.loads(body));pid='test-'+str(Handler.posts)
            Handler.history[pid]={'status':{'completed':True,'status_str':'success'},'outputs':{'2':{'videos':[{'filename':'clip.mp4','subfolder':'','type':'output'}]}}}
            if Handler.lose_reply:
                self.connection.shutdown(2);self.connection.close();return
            self.send({'prompt_id':pid,'number':0,'node_errors':{}})
        elif self.path=='/upload/image':self.send({'name':'uploaded.png','subfolder':'my-workflow','type':'input'})
        else:self.send({},404)

class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
        cls.media_tmp=tempfile.TemporaryDirectory()
        if shutil.which('ffmpeg'):
            p=Path(cls.media_tmp.name)/'fixture.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=green:s=160x96:r=24:d=1','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(p)],check=True)
            Handler.blob=p.read_bytes()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.media_tmp.cleanup()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);mw.init(self.root)
        Handler.posts=0;Handler.history={};Handler.submitted=[];Handler.lose_reply=False;Handler.guide_fail=False
        self.p=mw.new_project('测试短剧','目标和冲突')
        self.p['shots']=[{'id':'S01','title':'开场','profile':'dialogue','seconds':1,'prompt':'A clear one-second shot.','referenceAssets':[],'status':'planned'}]
        def config(s):
            s['config']['comfyUrl']=self.url;s['projects'].append(copy.deepcopy(self.p))
            s['profiles'][0].update(workflowGraph=copy.deepcopy(GRAPH),workflowName='实际导入图',bindings={'prompt':'1.prompt','duration':'1.seconds','seed':'1.seed'})
        mw.mutate(self.root,config)
    def tearDown(self):self.tmp.cleanup()
    def test_init_preserves_user_state(self):
        before=mw.load_state(self.root);mw.init(self.root);after=mw.load_state(self.root)
        self.assertEqual(before,after);self.assertTrue((self.root/'index.html').exists())
    def test_path_escape_rejected(self):
        with self.assertRaises(mw.WorkflowError):mw.safe(self.root,'../outside')
        s=mw.load_state(self.root);s['projects'][0]['assets']=[{'path':'../private'}]
        with self.assertRaises(mw.WorkflowError):mw.validate_state(s)
    def test_canvas_not_accepted_as_api(self):
        with self.assertRaises(mw.WorkflowError):mw.normalize_graph({'nodes':[]})
    def test_stage_templates_use_current_project_and_budget(self):
        s=mw.load_state(self.root)
        for item in mw.read_json(ROOT/'templates/prompts.json'):
            text=mw.prompt_text(s,item['id'],self.p['id']);self.assertNotIn('{{',text);self.assertIn(str(self.root),text);self.assertIn(self.p['id'],text)
    def test_preflight_is_read_only(self):
        before=mw.digest(self.root/'workspace.json');out=mw.preflight(self.root,self.p['id'])
        self.assertEqual(Handler.posts,0);self.assertFalse(out['submitted']);self.assertEqual(before,mw.digest(self.root/'workspace.json'))
    def test_missing_node_blocks_preflight(self):
        mw.mutate(self.root,lambda s:s['profiles'][0]['workflowGraph']['1'].update(class_type='MissingNode'))
        with self.assertRaises(mw.WorkflowError):mw.preflight(self.root,self.p['id'])
        self.assertEqual(Handler.posts,0)
    def test_submit_dedup_and_download_waiting_review(self):
        a=mw.run_project(self.root,self.p['id']);self.assertEqual(a['jobs'][0]['status'],'queued')
        mw.run_project(self.root,self.p['id']);self.assertEqual(Handler.posts,1)
        out=mw.poll(self.root,self.p['id']);self.assertEqual(out['updates'][0]['status'],'generated')
        s=mw.load_state(self.root)['projects'][0]['shots'][0]
        self.assertEqual(s['status'],'generated');self.assertNotIn('review',s)
        self.assertEqual(mw.safe(self.root,s['media']['path']).read_bytes(),Handler.blob)
        self.assertEqual(Handler.submitted[0]['prompt']['1']['inputs']['prompt'],'A clear one-second shot.')
    def test_lost_reply_never_blindly_reposts(self):
        Handler.lose_reply=True
        out=mw.run_project(self.root,self.p['id']);self.assertEqual(out['jobs'][0]['status'],'submission_unknown')
        mw.run_project(self.root,self.p['id']);self.assertEqual(Handler.posts,1)
    def test_batch_budget_stops_before_post(self):
        def change(s):s['config']['maxJobsPerRun']=1;s['projects'][0]['shots'].append(dict(s['projects'][0]['shots'][0],id='S02'))
        mw.mutate(self.root,change)
        with self.assertRaises(mw.WorkflowError):mw.run_project(self.root,self.p['id'])
        self.assertEqual(Handler.posts,0)
    def test_changed_prompt_cannot_duplicate_running_shot(self):
        mw.run_project(self.root,self.p['id'])
        mw.mutate(self.root,lambda s:s['projects'][0]['shots'][0].update(prompt='Changed while first job is active'))
        with self.assertRaises(mw.WorkflowError):mw.run_project(self.root,self.p['id'])
        self.assertEqual(Handler.posts,1)
    def test_cli_import_replaces_browser_embedded_graph(self):
        replacement=copy.deepcopy(GRAPH);replacement['1']['inputs']['prompt']='new graph'
        path=self.root/'replacement.json';mw.atomic_json(path,replacement)
        with redirect_stdout(io.StringIO()):mw.main(['workflow','import','--workspace',str(self.root),'--file',str(path),'--profile','dialogue','--prompt-slot','1.prompt','--duration-slot','1.seconds'])
        profile=mw.load_state(self.root)['profiles'][0]
        self.assertNotIn('workflowGraph',profile)
        self.assertEqual(mw.load_graph(mw.safe(self.root,profile['workflowPath']))['1']['inputs']['prompt'],'new graph')
    def test_system_curl_fallback_keeps_tls_verification(self):
        import ssl
        import urllib.error
        error=urllib.error.URLError(ssl.SSLCertVerificationError('local Python certificate store'))
        with patch.object(mw.urllib.request,'urlopen',side_effect=error),patch.object(mw.shutil,'which',return_value='/usr/bin/curl'):
            with patch.object(mw.Client,'curl_verified',return_value=b'{}') as fallback:
                self.assertEqual(mw.Client({'comfyUrl':self.url}).json('GET','/system_stats'),{})
                self.assertEqual(fallback.call_count,1)
    def test_static_duration_must_be_explicit(self):
        def change(s):s['profiles'][0]['bindings']['duration']=''
        mw.mutate(self.root,change)
        with self.assertRaises(mw.WorkflowError):mw.preflight(self.root,self.p['id'])
    def test_rule_and_project_isolation(self):
        second=mw.new_project('另一部剧');mw.mutate(self.root,lambda s:s['projects'].append(second))
        mw.mutate(self.root,lambda s:s['projects'][0].update(story='只改这部剧'))
        self.assertEqual(mw.load_state(self.root)['projects'][1]['story'],'')
        with redirect_stdout(io.StringIO()):mw.main(['rule','add','--workspace',str(self.root),'--title','自然表演','--content','说完闭嘴听对方','--project',self.p['id']])
        r=mw.load_state(self.root)['rules'][-1];self.assertEqual(r['projectId'],self.p['id']);self.assertIn('自然表演',(self.root/'rules.md').read_text(encoding='utf-8'))
    def test_import_checks_disk_revision(self):
        s=mw.load_state(self.root);incoming=copy.deepcopy(s);incoming['revision']=99999;incoming['config']['editor']='ffmpeg';p=self.root/'incoming.json';mw.atomic_json(p,incoming)
        with self.assertRaises(mw.WorkflowError):mw.main(['import-state','--workspace',str(self.root),'--file',str(p),'--expected-revision','0'])
        with redirect_stdout(io.StringIO()):mw.main(['import-state','--workspace',str(self.root),'--file',str(p),'--expected-revision',str(s['revision'])])
        self.assertEqual(mw.load_state(self.root)['revision'],s['revision']+1)
    def test_guide_failure_keeps_last_good_cache(self):
        with patch.object(mw,'OFFICIAL_ROOT',self.url+'/guide/'):
            first=mw.guide_refresh(self.root,mw.load_state(self.root)['config']);self.assertEqual(first['status'],'updated')
            old=mw.digest(self.root/'guides/SKILL.md');Handler.guide_fail=True
            second=mw.guide_refresh(self.root,mw.load_state(self.root)['config']);self.assertEqual(second['status'],'kept_previous');self.assertEqual(old,mw.digest(self.root/'guides/SKILL.md'))
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'media tools unavailable')
    def test_assembly_requires_review_and_current_hash(self):
        mw.run_project(self.root,self.p['id']);mw.poll(self.root,self.p['id'])
        with self.assertRaises(mw.WorkflowError):mw.assemble(self.root,self.p['id'])
        with redirect_stdout(io.StringIO()):mw.main(['review','--workspace',str(self.root),'--project',self.p['id'],'--shot-id','S01','--status','passed','--note','Synthetic fixture checked for this test','--out','0.75'])
        out=mw.assemble(self.root,self.p['id']);self.assertTrue(mw.safe(self.root,out['path']).exists());self.assertEqual(out['technical']['status'],'passed')
        media=mw.load_state(self.root)['projects'][0]['shots'][0]['media'];mw.safe(self.root,media['path']).write_bytes(b'changed')
        with self.assertRaises(mw.WorkflowError):mw.assemble(self.root,self.p['id'])

if __name__=='__main__':unittest.main()
