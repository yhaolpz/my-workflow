#!/usr/bin/env python3
"""Portable, standard-library-only tools for the My Workflow agent skill."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

PACKAGE = Path(__file__).resolve().parents[1]
OFFICIAL_ROOT = 'https://raw.githubusercontent.com/MiniMax-AI/MiniMax-H3/main/skills/h3-prompt-writing/'
ACTIVE = {'prepared', 'queued', 'running', 'submission_unknown'}


class WorkflowError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe(root, relative):
    root = Path(root).resolve()
    p = (root / str(relative)).resolve()
    if not p.is_relative_to(root):
        raise WorkflowError('文件路径超出工作目录')
    return p


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.mw-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink()


def workspace_arg(args):
    return Path(args.workspace or os.environ.get('MY_WORKFLOW_HOME') or Path.home() / 'MyWorkflow').expanduser().resolve()


@contextmanager
def lock(root):
    p = root / '.mw.lock'
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise WorkflowError('工作目录正在写入。等待当前任务结束；异常退出留下的 .mw.lock 请核对进程后处理。')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({'pid': os.getpid(), 'at': now()}, f)
        yield
    finally:
        p.unlink(missing_ok=True)


def load_state(root):
    p = root / 'workspace.json'
    if not p.exists():
        raise WorkflowError('工作目录尚未初始化，请先运行 init')
    data = read_json(p)
    validate_state(data)
    return data


def validate_state(data):
    if not isinstance(data, dict) or data.get('schemaVersion') != 1:
        raise WorkflowError('不支持的工作区格式')
    for key in ('projects', 'profiles', 'rules'):
        if not isinstance(data.get(key), list):
            raise WorkflowError(f'{key} 必须是列表')
    if not isinstance(data.get('config'), dict):
        raise WorkflowError('缺少生成配置')
    ids = [p.get('id') for p in data['projects']]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r'[a-zA-Z0-9_-]+', str(x or '')) for x in ids):
        raise WorkflowError('项目 ID 缺失、重复或含路径字符')
    for p in data['projects']:
        for key in ('assets', 'shots', 'jobs'):
            if not isinstance(p.get(key), list):
                raise WorkflowError(f'项目 {p["id"]} 缺少 {key} 列表')
        shots = [s.get('id') for s in p['shots']]
        if len(shots) != len(set(shots)) or any(not re.fullmatch(r'[a-zA-Z0-9_-]+', str(x or '')) for x in shots):
            raise WorkflowError('镜头 ID 缺失、重复或含路径字符')
        for a in p['assets']:
            rel = str(a.get('path', ''))
            if Path(rel).is_absolute() or '..' in Path(rel).parts or '\\' in rel:
                raise WorkflowError('资产路径必须是工作区内相对路径')
    for key, limit in [('maxJobsPerRun', 100), ('maxSecondsPerRun', 3600), ('maxAttempts', 10)]:
        value = data['config'].get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 1 <= value <= limit:
            raise WorkflowError(f'{key} 超出允许范围 1–{limit}')


def snapshot(root, state):
    # A normal script tag works under file://; no fetch, server, or localStorage bridge.
    p = root / 'workspace.snapshot.js'
    p.write_text('window.MW_WORKSPACE = ' + json.dumps(state, ensure_ascii=False).replace('<', '\\u003c') + ';\n', encoding='utf-8')
    rules = ['# 我的工作流个人规则', '']
    for r in state['rules']:
        if r.get('enabled', True):
            rules += ['## ' + r['title'], f'范围：{r.get("scope", "global")} {r.get("projectId", "")}', r['content'], '']
    (root / 'rules.md').write_text('\n'.join(rules), encoding='utf-8')


def save(root, state):
    validate_state(state)
    state['revision'] = int(state.get('revision', 0)) + 1
    state['updatedAt'] = now()
    atomic_json(root / 'workspace.json', state)
    snapshot(root, state)


def mutate(root, fn):
    with lock(root):
        s = load_state(root)
        result = fn(s)
        save(root, s)
        return result


def project(state, pid):
    p = next((p for p in state['projects'] if p['id'] == pid), None)
    if p is None:
        raise WorkflowError(f'没有项目 {pid}')
    return p


def shot_in(p, sid):
    shot = next((s for s in p['shots'] if s['id'] == sid), None)
    if shot is None:
        raise WorkflowError(f'没有镜头 {sid}')
    return shot


def new_project(name, idea=''):
    return {'id': 'p-' + uuid.uuid4().hex[:10], 'name': name, 'logline': idea, 'stage': 1,
            'story': '', 'createdAt': now(), 'assets': [], 'shots': [], 'jobs': [], 'deliveries': []}


def init(root, demo=False):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not (root / 'workspace.json').exists():
        s = read_json(PACKAGE / 'templates/default-state.json')
        s.update(workspaceId=uuid.uuid4().hex, workspaceRoot=str(root))
        if demo:
            p = new_project('山海修补铺', '一间普通改衣店，藏着上古神话的秘密。')
            p.update(isDemo=True, story='示例大纲：现代改衣店的日常与神话危机并行。先确认人物关系和主要冲突，再拆分镜。')
            p['shots'] = [{'id': 'S01', 'title': '改衣店开门', 'profile': 'dialogue', 'seconds': 10,
                           'prompt': '', 'referenceAssets': [], 'status': 'planned'},
                          {'id': 'S02', 'title': '神秘来客', 'profile': 'dialogue', 'seconds': 10,
                           'prompt': '', 'referenceAssets': [], 'status': 'planned'},
                          {'id': 'S03', 'title': '天空裂隙', 'profile': 'action', 'seconds': 10,
                           'prompt': '', 'referenceAssets': [], 'status': 'planned'}]
            s['projects'] = [p, dict(new_project('夜班便利店', '凌晨三点，店里来了一位不寻常的客人。'), isDemo=True)]
        with lock(root):
            save(root, s)
    for folder in ['projects', 'workflows', 'jobs', 'guides']:
        (root / folder).mkdir(exist_ok=True)
    for src in (PACKAGE / 'assets/workstation').iterdir():
        if src.is_file():
            shutil.copy2(src, root / src.name)
    s = load_state(root)
    if s.get('workspaceRoot') != str(root):
        with lock(root):
            s['workspaceRoot'] = str(root)
            save(root, s)
    else:
        snapshot(root, s)
    return {'workspace': str(root), 'page': str(root / 'index.html'), 'projects': len(s['projects'])}


def normalize_graph(data):
    if not isinstance(data, dict) or 'nodes' in data:
        raise WorkflowError('请导入 API 格式 JSON，画布 JSON 不能直接提交')
    graph = data.get('prompt', data)
    if not isinstance(graph, dict) or not graph or not all(isinstance(n, dict) and isinstance(n.get('class_type'), str) and isinstance(n.get('inputs'), dict) for n in graph.values()):
        raise WorkflowError('API 图需要由 class_type 和 inputs 构成的节点映射')
    return graph


def load_graph(path):
    return normalize_graph(read_json(path))


def slot_value(graph, slot, value=None, write=False):
    if '.' not in slot:
        raise WorkflowError('槽位格式为 节点ID.输入名')
    nid, key = slot.split('.', 1)
    if nid not in graph or key not in graph[nid]['inputs']:
        raise WorkflowError(f'工作流中不存在槽位 {slot}')
    old = graph[nid]['inputs'][key]
    if isinstance(old, list):
        raise WorkflowError(f'{slot} 是节点连线，不能覆盖为普通参数')
    if write:
        graph[nid]['inputs'][key] = value
    return old


def graph_slots(graph):
    return [{'slot': f'{nid}.{key}', 'class': n['class_type'], 'value': v}
            for nid, n in graph.items() for key, v in n['inputs'].items()
            if isinstance(v, (str, int, float, bool))]


def base_url(value):
    p = urllib.parse.urlsplit(value or '')
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise WorkflowError('ComfyUI 地址应为不含密码、密钥参数或片段的 HTTP(S) 基础地址')
    return value.rstrip('/')


class Client:
    def __init__(self, config):
        self.base = base_url(config.get('comfyUrl'))
        self.headers = {}
        env = config.get('tokenEnv', '').strip()
        if env:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env):
                raise WorkflowError('认证环境变量名无效')
            token = os.environ.get(env)
            if not token:
                raise WorkflowError(f'运行环境未设置 {env}')
            self.headers['Authorization'] = 'Bearer ' + token

    def raw(self, method, route, payload=None, headers=None, timeout=30):
        req = urllib.request.Request(self.base + route, data=payload, method=method,
                                     headers={**self.headers, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            # Do not print response bodies: authentication proxies may include secrets.
            raise WorkflowError(f'ComfyUI HTTP {e.code}，检查地址、认证与工作流输入') from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError) and shutil.which('curl'):
                return self.curl_verified(method, route, payload, headers or {}, timeout)
            raise WorkflowError(f'ComfyUI 请求未完成（{type(e).__name__}），请查连接；POST 不自动重发') from e
        except (TimeoutError, OSError) as e:
            raise WorkflowError(f'ComfyUI 请求未完成（{type(e).__name__}），请查连接；POST 不自动重发') from e

    def curl_verified(self, method, route, payload, headers, timeout):
        # Some macOS Python builds do not use the system certificate store.
        # Curl keeps normal TLS verification; credentials stay out of argv/logs.
        def quoted(value):
            if '\n' in value or '\r' in value:
                raise WorkflowError('请求配置含无效换行')
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
        with tempfile.TemporaryDirectory(prefix='my-workflow-http-') as folder:
            folder = Path(folder); config = folder / 'request.conf'; output = folder / 'response.bin'
            lines = ['url = ' + quoted(self.base + route), 'request = ' + quoted(method),
                     'silent', 'show-error', 'connect-timeout = 5', 'max-time = ' + str(timeout),
                     'output = ' + quoted(str(output)), 'write-out = "%{http_code}"']
            for key, value in {**self.headers, **headers}.items():
                lines.append('header = ' + quoted(key + ': ' + value))
            if payload is not None:
                body = folder / 'request.bin'; body.write_bytes(payload)
                lines.append('data-binary = ' + quoted('@' + str(body)))
            config.write_text('\n'.join(lines), encoding='utf-8'); config.chmod(0o600)
            run = subprocess.run([shutil.which('curl'), '--disable', '--config', str(config)], capture_output=True, text=True)
            if run.returncode or not run.stdout.strip().isdigit():
                raise WorkflowError('系统 TLS 请求失败；未跳过证书校验，也不会自动重发 POST')
            status = int(run.stdout.strip())
            if not 200 <= status < 300:
                raise WorkflowError(f'ComfyUI HTTP {status}，检查认证或入口')
            return output.read_bytes()

    def json(self, method, route, data=None):
        raw = self.raw(method, route, json.dumps(data).encode() if data is not None else None,
                       {'Content-Type': 'application/json'} if data is not None else {})
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise WorkflowError('服务没有返回 JSON，可能是登录页或错误入口')

    def upload(self, path):
        path = Path(path)
        boundary = 'mw-' + uuid.uuid4().hex
        name = hashlib.sha256(path.read_bytes()).hexdigest()[:24] + path.suffix.lower()
        parts = []
        def field(key, value):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        field('type', 'input'); field('subfolder', 'my-workflow'); field('overwrite', 'false')
        typ = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: {typ}\r\n\r\n'.encode())
        parts += [path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()]
        data = json.loads(self.raw('POST', '/upload/image', b''.join(parts), {'Content-Type': f'multipart/form-data; boundary={boundary}'}))
        return (data.get('subfolder', '').strip('/') + '/' + data['name']).lstrip('/')


def validate_graph(graph, catalog, ignored=()):
    errors = []
    deferred = []
    for nid, node in graph.items():
        schema = catalog.get(node['class_type'])
        if not schema:
            errors.append(f'{nid}: 缺少节点 {node["class_type"]}'); continue
        required = schema.get('input', {}).get('required', {})
        specs = {**required, **schema.get('input', {}).get('optional', {})}
        for key, spec in required.items():
            if spec and spec[0] == 'COMFY_AUTOGROW_V3':
                deferred.append(f'{nid}.{key}'); continue
            if key not in node['inputs']:
                errors.append(f'{nid}.{key}: 缺少必填参数')
        for key, val in node['inputs'].items():
            spec = specs.get(key)
            if isinstance(val, list) and len(val) == 2 and isinstance(val[1], int):
                src = graph.get(str(val[0]))
                outs = catalog.get(src['class_type'], {}).get('output', []) if src else []
                if src is None or not 0 <= val[1] < len(outs):
                    errors.append(f'{nid}.{key}: 无效节点连线')
            elif f'{nid}.{key}' not in ignored and spec and isinstance(spec[0], list) and val not in spec[0]:
                errors.append(f'{nid}.{key}: 模型或选项不在当前实例列表中')
    if not any(catalog.get(n['class_type'], {}).get('output_node') for n in graph.values()):
        errors.append('工作流没有输出节点')
    if errors:
        raise WorkflowError('\n'.join(errors))
    return {'nodes': len(graph), 'dynamicInputsDeferred': deferred, 'serverValidationRequired': True}


def prepare(root, state, p, shot, upload=False, client=None):
    profile = next((x for x in state['profiles'] if x['id'] == shot.get('profile', 'dialogue')), None)
    if not profile or not (profile.get('workflowPath') or profile.get('workflowGraph')):
        raise WorkflowError(f'{shot["id"]}: 尚未导入 {shot.get("profile", "dialogue")} 工作流')
    graph = copy.deepcopy(normalize_graph(profile['workflowGraph']) if profile.get('workflowGraph') else load_graph(safe(root, profile['workflowPath'])))
    binds = profile.get('bindings', {})
    text = shot.get('prompt', '').strip()
    if not text:
        raise WorkflowError(f'{shot["id"]}: 缺少完整生成提示词')
    if not binds.get('prompt'):
        raise WorkflowError('尚未绑定提示词节点槽位')
    if not isinstance(slot_value(graph, binds['prompt']), str):
        raise WorkflowError('提示词必须绑定到文本输入')
    slot_value(graph, binds['prompt'], text, True)
    seconds = float(shot.get('seconds', 10))
    if not 0 < seconds <= 60:
        raise WorkflowError('镜头时长必须在 0–60 秒内，并符合实际模型限制')
    if binds.get('duration'):
        old = slot_value(graph, binds['duration'])
        if not isinstance(old, (int, float)) or isinstance(old, bool):
            raise WorkflowError('时长槽位不是数值；帧数型节点请使用固定时长并配置原图')
        if isinstance(old, int) and seconds != int(seconds):
            raise WorkflowError('当前时长节点要求整数秒')
        slot_value(graph, binds['duration'], int(seconds) if isinstance(old, int) else seconds, True)
    elif abs(float(profile.get('fixedSeconds', 0)) - seconds) > .001:
        raise WorkflowError('未绑定时长：请填写原图的真实固定秒数，或绑定秒数输入；不能猜帧数规则')
    for slot, value in profile.get('overrides', {}).items():
        if slot in [binds.get('prompt'), binds.get('duration'), binds.get('seed')]:
            raise WorkflowError('参数覆盖不能覆盖已绑定的提示词、时长或种子')
        slot_value(graph, slot, value, True)
    refs = shot.get('referenceAssets', [])
    rb = profile.get('referenceBindings', [])
    if refs and not rb:
        raise WorkflowError('本镜有参考素材但未配置参考槽位')
    reference_hashes = []
    ignored = []
    used = set()
    for binding in rb:
        index = int(binding.get('index', 0))
        if index < 0 or index >= len(refs):
            raise WorkflowError('参考槽位的序号超出本镜素材数量')
        a = next((a for a in p['assets'] if a['id'] == refs[index]), None)
        if not a:
            raise WorkflowError(f'找不到参考资产 {refs[index]}')
        f = safe(root, a['path'])
        if not f.is_file():
            raise WorkflowError(f'参考文件不存在：{a["path"]}')
        reference_hashes.append({'index': index, 'sha256': digest(f)})
        slot_value(graph, binding['slot'])
        ignored.append(binding['slot']); used.add(index)
        if upload:
            slot_value(graph, binding['slot'], client.upload(f), True)
    if used != set(range(len(refs))):
        raise WorkflowError('有参考素材未绑定，不能静默丢弃')
    fingerprint = hashlib.sha256(json.dumps({'graph': graph, 'refs': reference_hashes, 'shot': shot['id']}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return graph, profile, fingerprint, ignored


def file_descriptors(item):
    out = []; seen = set()
    for nid, node in item.get('outputs', {}).items():
        if not isinstance(node, dict):
            continue
        for kind in ('images', 'gifs', 'videos', 'audio'):
            values = node.get(kind, [])
            if isinstance(values, dict): values = [values]
            for v in values if isinstance(values, list) else []:
                if not isinstance(v, dict) or not v.get('filename'): continue
                d = {'filename': v['filename'], 'subfolder': v.get('subfolder', ''), 'type': v.get('type', 'output')}
                key = json.dumps(d, sort_keys=True)
                if key not in seen: out.append(d); seen.add(key)
    return out


def media_check(path, full=True):
    if not shutil.which('ffprobe'):
        return {'status': 'pending', 'reason': 'ffprobe 未安装'}
    run = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], capture_output=True, text=True)
    if run.returncode:
        return {'status': 'failed', 'reason': '无法探测媒体'}
    data = json.loads(run.stdout)
    streams = data.get('streams', [])
    result = {'status': 'passed', 'duration': float(data.get('format', {}).get('duration', 0)),
              'streams': [{k: s[k] for k in ('codec_type', 'codec_name', 'width', 'height', 'r_frame_rate', 'sample_rate', 'channels') if k in s} for s in streams]}
    if full:
        if not shutil.which('ffmpeg'):
            result.update(status='pending', reason='ffmpeg 未安装，全片解码待检查')
        else:
            dec = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'], capture_output=True, text=True)
            if dec.returncode or dec.stderr.strip(): result.update(status='failed', reason='严格解码有错误')
    return result


def preflight(root, pid, sid=None):
    s = load_state(root); p = project(s, pid); c = Client(s['config'])
    catalog = c.json('GET', '/object_info')
    shots = [shot_in(p, sid)] if sid else p['shots']
    if not shots: raise WorkflowError('项目没有镜头')
    results = []
    for shot in shots:
        graph, profile, fingerprint, ignored = prepare(root, s, p, shot)
        results.append({'shot': shot['id'], 'profile': profile['id'], 'fingerprint': fingerprint, **validate_graph(graph, catalog, ignored)})
    return {'status': 'preflight_passed', 'shots': results, 'submitted': False}


def run_project(root, pid, sid=None, retry=False):
    state = load_state(root); p = project(state, pid)
    selected = [shot_in(p, sid)] if sid else p['shots']
    prepared = []
    for shot in selected:
        graph, profile, fingerprint, ignored = prepare(root, state, p, shot)
        existing = [j for j in p['jobs'] if j.get('fingerprint') == fingerprint]
        if existing and not retry:
            continue
        if any(j.get('shotId') == shot['id'] and j['status'] in ACTIVE for j in p['jobs']):
            raise WorkflowError('本镜仍有排队、运行或状态不明任务，请先查询原任务')
        attempts = sum(j['shotId'] == shot['id'] for j in p['jobs'])
        if attempts >= int(state['config']['maxAttempts']):
            raise WorkflowError(f'{shot["id"]} 达到重做上限，请分析原因并调整计划')
        prepared.append((shot, graph, profile, fingerprint))
    if len(prepared) > state['config']['maxJobsPerRun'] or sum(float(x[0]['seconds']) for x in prepared) > state['config']['maxSecondsPerRun']:
        raise WorkflowError('本批超出已配置任务数或生成总秒数预算，请缩小范围或明确调整预算')
    c = Client(state['config']); results = []
    for shot, _, _, fingerprint in prepared:
        jid = 'j-' + uuid.uuid4().hex
        job = {'id': jid, 'shotId': shot['id'], 'fingerprint': fingerprint, 'status': 'prepared', 'createdAt': now(), 'promptId': None, 'files': []}
        def reserve(s):
            current = project(s, pid)
            if any(j.get('shotId') == shot['id'] and j['status'] in ACTIVE for j in current['jobs']):
                raise WorkflowError('另一个进程已提交本镜')
            current['jobs'].append(copy.deepcopy(job)); shot_in(current, shot['id'])['status'] = 'prepared'
        mutate(root, reserve)
        jobdir = safe(root, 'jobs/' + jid); jobdir.mkdir(parents=True, exist_ok=True)
        try:
            graph, profile, _, _ = prepare(root, state, p, shot, True, c)
            catalog = c.json('GET', '/object_info')
            validate_graph(graph, catalog)
            seed_slot = profile.get('bindings', {}).get('seed')
            if seed_slot:
                slot_value(graph, seed_slot, int.from_bytes(os.urandom(4), 'little'), True)
            atomic_json(jobdir / 'workflow.api.json', graph)
            atomic_json(jobdir / 'request.json', {'project': pid, 'shot': shot, 'fingerprint': fingerprint, 'referenceSha256': [digest(safe(root, a['path'])) for a in p['assets'] if a['id'] in shot.get('referenceAssets', [])]})
            job['status'] = 'submission_unknown'
            # Durable uncertain state is written BEFORE POST. A lost reply is not retried.
            mutate(root, lambda s: next(j for j in project(s, pid)['jobs'] if j['id'] == jid).update(job))
            reply = c.json('POST', '/prompt', {'prompt': graph, 'client_id': jid})
            if not reply.get('prompt_id') or reply.get('node_errors'):
                job.update(status='failed', reason='服务端未通过校验')
            else:
                job.update(status='queued', promptId=reply['prompt_id'])
        except Exception as e:
            job['reason'] = str(e) if isinstance(e, WorkflowError) else type(e).__name__
            if job['status'] == 'prepared': job['status'] = 'failed'
        atomic_json(jobdir / 'receipt.json', job)
        def finish(s):
            cur = project(s, pid)
            next(j for j in cur['jobs'] if j['id'] == jid).update(job)
            shot_in(cur, shot['id'])['status'] = job['status']
        mutate(root, finish); results.append(job)
        if job['status'] in ('submission_unknown', 'failed'): break
    return {'jobs': results, 'skippedExisting': len(selected) - len(prepared)}


def poll(root, pid):
    root = Path(root).resolve()
    state = load_state(root); p = project(state, pid); c = Client(state['config']); updates = []
    for original in p['jobs']:
        if original['status'] not in ACTIVE or not original.get('promptId'): continue
        job = copy.deepcopy(original)
        history = c.json('GET', '/history/' + urllib.parse.quote(job['promptId'], safe=''))
        item = history.get(job['promptId'])
        if not item: continue
        status = item.get('status', {})
        if status.get('status_str') == 'error':
            job.update(status='failed', reason='ComfyUI 执行失败')
        elif status.get('completed') and status.get('status_str') == 'success':
            descriptors = file_descriptors(item)
            if not descriptors:
                job.update(status='failed', reason='任务完成但没有可下载产物')
            else:
                folder = safe(root, f'projects/{pid}/generated/{job["id"]}'); folder.mkdir(parents=True, exist_ok=True)
                files = []
                for d in descriptors:
                    name = Path(d['filename'].replace('\\', '/')).name
                    if name in ('', '.', '..'): raise WorkflowError('服务端返回无效文件名')
                    dest = folder / name
                    if not dest.exists():
                        raw = c.raw('GET', '/view?' + urllib.parse.urlencode(d))
                        with dest.open('xb') as out: out.write(raw)
                    entry = {'path': str(dest.relative_to(root)).replace('\\', '/'), 'sha256': digest(dest), 'descriptor': d}
                    if dest.suffix.lower() in ('.mp4', '.mov', '.webm', '.mkv', '.wav', '.mp3', '.flac'):
                        entry['technical'] = media_check(dest)
                    files.append(entry)
                job.update(status='generated', files=files, completedAt=now())
        else:
            job['status'] = 'running'
        atomic_json(safe(root, f'jobs/{job["id"]}/receipt.json'), job)
        def apply(s):
            cur = project(s, pid)
            next(j for j in cur['jobs'] if j['id'] == job['id']).update(job)
            shot = shot_in(cur, job['shotId']); shot['status'] = job['status']
            video = next((f for f in job['files'] if Path(f['path']).suffix.lower() in ('.mp4', '.webm', '.mov', '.mkv')), None)
            if video:
                shot['media'] = video
                shot.pop('review', None)
        mutate(root, apply); updates.append(job)
    return {'updates': updates, 'note': 'generated 表示已下载，画面仍待审片'}


def prompt_text(state, template_id, pid=None):
    templates = read_json(PACKAGE / 'templates/prompts.json')
    template = next((p for p in templates if p['id'] == template_id), None)
    if not template: raise WorkflowError('未知阶段模板')
    p = project(state, pid) if pid else {'id': '请选择项目', 'name': '当前项目'}
    config = state['config']
    values = {'workspace': state['workspaceRoot'], 'projectId': p['id'], 'projectName': p['name'],
              'budget': f'本批最多 {config["maxJobsPerRun"]} 个任务、{config["maxSecondsPerRun"]} 秒，单镜最多尝试 {config["maxAttempts"]} 次'}
    text = template['body']
    for k, v in values.items(): text = text.replace('{{' + k + '}}', str(v))
    return text


def guide_refresh(root, config):
    link = config.get('guideUrl', '')
    official = 'https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/SKILL.md'
    if link != official:
        raise WorkflowError('自动更新仅允许已确认的 MiniMax 官方指南；其他链接请由助手先核对来源')
    folder = root / 'guides'; folder.mkdir(exist_ok=True)
    downloaded = []
    # Download the complete set first, so a network failure does not replace last-good cache.
    try:
        client = Client({'comfyUrl': OFFICIAL_ROOT.rstrip('/'), 'tokenEnv': ''})
        for relative in ['SKILL.md', 'references/base-en.txt', 'references/ref-en.txt']:
            raw = client.raw('GET', '/' + relative)
            text = raw.decode('utf-8')
            if not text.strip() or '<html' in text[:200].lower(): raise WorkflowError('指南返回了空内容或 HTML')
            downloaded.append((relative, text))
    except Exception as e:
        previous = folder / 'manifest.json'
        return {'status': 'kept_previous' if previous.exists() else 'unavailable', 'reason': type(e).__name__, 'officialUrl': link,
                'previous': read_json(previous) if previous.exists() else None}
    items = []
    for relative, text in downloaded:
        path = safe(folder, relative); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding='utf-8')
        items.append({'path': relative, 'source': OFFICIAL_ROOT + relative, 'sha256': digest(path)})
    manifest = {'updatedAt': now(), 'officialUrl': link, 'files': items}
    atomic_json(folder / 'manifest.json', manifest)
    return {'status': 'updated', **manifest}


def assemble(root, pid):
    root = Path(root).resolve()
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise WorkflowError('基础拼接需要 FFmpeg 和 ffprobe；请让助手按操作系统配置')
    state = load_state(root); p = project(state, pid)
    if not p['shots']: raise WorkflowError('没有镜头')
    selected = []
    for shot in p['shots']:
        media = shot.get('media', {}); review = shot.get('review', {})
        path = safe(root, media.get('path', '.'))
        if review.get('status') != 'passed' or not path.is_file() or digest(path) != review.get('sha256'):
            raise WorkflowError(f'{shot["id"]} 未通过当前源文件的审片，不能采用')
        meta = media_check(path, full=False)
        if meta['status'] != 'passed': raise WorkflowError('无法确认素材参数')
        stream = next((x for x in meta['streams'] if x['codec_type'] == 'video'), None)
        if not stream: raise WorkflowError('选段没有视频轨')
        start = float(review.get('in', 0)); end = float(review.get('out') or meta['duration'])
        if start < 0 or end <= start or end > meta['duration'] + .05: raise WorkflowError('审片取用范围无效')
        selected.append((shot, path, meta, stream, start, end))
    size = (selected[0][3]['width'], selected[0][3]['height'])
    if any((x[3]['width'], x[3]['height']) != size for x in selected):
        raise WorkflowError('基础拼接要求画面尺寸一致；需要重构图时请由助手建立剪辑工程')
    folder = safe(root, f'projects/{pid}/exports/{uuid.uuid4().hex[:10]}'); folder.mkdir(parents=True)
    pieces = []
    for index, (_, path, meta, stream, start, end) in enumerate(selected):
        part = folder / f'part-{index:03}.mp4'
        cmd = ['ffmpeg', '-v', 'error', '-ss', str(start), '-i', str(path)]
        has_audio = any(s['codec_type'] == 'audio' for s in meta['streams'])
        if not has_audio: cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
        cmd += ['-t', str(end - start), '-map', '0:v:0', '-map', '0:a:0' if has_audio else '1:a:0', '-r', '24', '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', '-ac', '2', str(part)]
        subprocess.run(cmd, check=True); pieces.append(part)
    listing = folder / 'concat.txt'
    listing.write_text(''.join(f"file '{p.name}'\n" for p in pieces), encoding='utf-8')
    final = folder / 'final.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'concat', '-safe', '1', '-i', str(listing), '-c', 'copy', '-movflags', '+faststart', str(final)], check=True)
    technical = media_check(final)
    if technical['status'] != 'passed': raise WorkflowError('导出文件技术检查未通过')
    result = {'path': str(final.relative_to(root)), 'sha256': digest(final), 'technical': technical, 'createdAt': now(), 'userAcceptance': 'pending'}
    mutate(root, lambda s: project(s, pid)['deliveries'].append(result))
    return result


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--workspace', help='数据目录，默认 ~/MyWorkflow')
    p = argparse.ArgumentParser(description='我的工作流：静态工作台与 ComfyUI 执行工具')
    sub = p.add_subparsers(dest='command', required=True)
    x = sub.add_parser('init', parents=[common]); x.add_argument('--open', action='store_true'); x.add_argument('--demo', action='store_true')
    for name in ['status', 'refresh', 'doctor']:
        sub.add_parser(name, parents=[common])
    x = sub.add_parser('project', parents=[common]); x.add_argument('action', choices=['add', 'list']); x.add_argument('--name'); x.add_argument('--idea', default='')
    x = sub.add_parser('asset', parents=[common]); x.add_argument('--project', required=True); x.add_argument('--file', required=True); x.add_argument('--title'); x.add_argument('--kind', default='reference')
    x = sub.add_parser('shot', parents=[common]); x.add_argument('--project', required=True); x.add_argument('--file', required=True, help='包含 id,title,profile,seconds,prompt,referenceAssets 的 JSON')
    x = sub.add_parser('workflow', parents=[common]); x.add_argument('action', choices=['inspect', 'import']); x.add_argument('--file', required=True); x.add_argument('--profile', default='dialogue'); x.add_argument('--name'); x.add_argument('--prompt-slot'); x.add_argument('--duration-slot'); x.add_argument('--seed-slot'); x.add_argument('--fixed-seconds', type=float, default=0)
    for name in ['preflight', 'run', 'poll']:
        x = sub.add_parser(name, parents=[common]); x.add_argument('--project', required=True); x.add_argument('--shot-id')
        if name == 'run': x.add_argument('--execute', action='store_true'); x.add_argument('--retry', action='store_true')
    x = sub.add_parser('review', parents=[common]); x.add_argument('--project', required=True); x.add_argument('--shot-id', required=True); x.add_argument('--status', choices=['passed', 'failed'], required=True); x.add_argument('--note', required=True); x.add_argument('--in', dest='start', type=float, default=0); x.add_argument('--out', dest='end', type=float); x.add_argument('--evidence', default='')
    x = sub.add_parser('assemble', parents=[common]); x.add_argument('--project', required=True); x.add_argument('--execute', action='store_true')
    x = sub.add_parser('rule', parents=[common]); x.add_argument('action', choices=['add', 'list']); x.add_argument('--title'); x.add_argument('--content'); x.add_argument('--category', choices=['experience', 'taste'], default='experience'); x.add_argument('--project')
    x = sub.add_parser('prompt', parents=[common]); x.add_argument('--id', required=True); x.add_argument('--project')
    x = sub.add_parser('guide', parents=[common]); x.add_argument('action', choices=['refresh', 'status'])
    x = sub.add_parser('import-state', parents=[common]); x.add_argument('--file', required=True); x.add_argument('--expected-revision', type=int, required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv); root = workspace_arg(args)
    if args.command == 'init':
        result = init(root, args.demo)
        if args.open: webbrowser.open((root / 'index.html').as_uri())
    elif args.command == 'refresh': result = init(root)
    elif args.command == 'status':
        s = load_state(root)
        result = {'workspace': str(root), 'revision': s['revision'], 'projects': [{'id': p['id'], 'name': p['name'], 'shots': len(p['shots']), 'jobs': len(p['jobs'])} for p in s['projects']]}
    elif args.command == 'doctor':
        s = load_state(root); c = Client(s['config']); stats = c.json('GET', '/system_stats'); nodes = c.json('GET', '/object_info')
        result = {'connection': 'reachable', 'nodes': len(nodes), 'python': sys.version.split()[0], 'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')), 'submitted': False}
    elif args.command == 'project':
        if args.action == 'list': result = load_state(root)['projects']
        else:
            if not args.name: raise WorkflowError('请填写项目名')
            result = new_project(args.name, args.idea); mutate(root, lambda s: s['projects'].append(result))
    elif args.command == 'asset':
        src = Path(args.file).expanduser().resolve()
        if not src.is_file(): raise WorkflowError('找不到素材文件')
        state = load_state(root); project(state, args.project)
        aid = 'a-' + uuid.uuid4().hex[:12]; extension = src.suffix.lower()
        dest = safe(root, f'projects/{args.project}/assets/{aid}{extension}'); dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dest)
        result = {'id': aid, 'title': args.title or src.name, 'kind': args.kind, 'path': str(dest.relative_to(root)).replace('\\', '/'), 'sha256': digest(dest)}
        mutate(root, lambda s: project(s, args.project)['assets'].append(result))
    elif args.command == 'shot':
        incoming = read_json(args.file)
        if not isinstance(incoming, list): incoming = [incoming]
        def change(s):
            p = project(s, args.project)
            for sh in incoming:
                if not isinstance(sh, dict) or not sh.get('id'): raise WorkflowError('镜头需要稳定 ID')
                current = next((x for x in p['shots'] if x['id'] == sh['id']), None)
                if current:
                    current.update(sh); current['status'] = 'planned'; current.pop('review', None)
                else:
                    sh.setdefault('status', 'planned'); sh.setdefault('referenceAssets', []); p['shots'].append(sh)
        mutate(root, change); result = {'saved': len(incoming)}
    elif args.command == 'workflow':
        graph = load_graph(args.file)
        if args.action == 'inspect': result = {'slots': graph_slots(graph)}
        else:
            state = load_state(root); pf = next((x for x in state['profiles'] if x['id'] == args.profile), None)
            if not pf: raise WorkflowError('未知预设')
            rel = f'workflows/{args.profile}-{uuid.uuid4().hex[:8]}.api.json'; atomic_json(safe(root, rel), graph)
            for sl in [args.prompt_slot, args.duration_slot, args.seed_slot]:
                if sl: slot_value(graph, sl)
            def change(s):
                x = next(x for x in s['profiles'] if x['id'] == args.profile)
                x.update(workflowPath=rel, workflowName=args.name or Path(args.file).stem, fixedSeconds=args.fixed_seconds)
                x.pop('workflowGraph', None)
                x['referenceBindings'] = []
                x['overrides'] = {}
                x['bindings'] = {'prompt': args.prompt_slot or '', 'duration': args.duration_slot or '', 'seed': args.seed_slot or ''}
                x['slots'] = graph_slots(graph)
            mutate(root, change); result = {'imported': rel, 'editableSlots': len(graph_slots(graph))}
    elif args.command == 'preflight': result = preflight(root, args.project, args.shot_id)
    elif args.command == 'run':
        if not args.execute: raise WorkflowError('实际生成需显式 --execute；只检查请用 preflight')
        result = run_project(root, args.project, args.shot_id, args.retry)
    elif args.command == 'poll': result = poll(root, args.project)
    elif args.command == 'review':
        def change(s):
            shot = shot_in(project(s, args.project), args.shot_id); media = shot.get('media', {})
            path = safe(root, media.get('path', '.'))
            if not path.is_file(): raise WorkflowError('镜头没有可检查的原片')
            if args.end is not None and args.end <= args.start: raise WorkflowError('取用区间无效')
            shot['review'] = {'status': args.status, 'note': args.note, 'sha256': digest(path), 'in': args.start, 'out': args.end, 'evidence': args.evidence, 'checkedAt': now()}
            shot['status'] = 'reviewed' if args.status == 'passed' else 'rejected'
            return shot['review']
        result = mutate(root, change)
    elif args.command == 'assemble':
        if not args.execute: raise WorkflowError('导出基础拼接需显式 --execute')
        result = assemble(root, args.project)
    elif args.command == 'rule':
        if args.action == 'list': result = load_state(root)['rules']
        else:
            if not args.title or not args.content: raise WorkflowError('规则需要标题和内容')
            if args.project: project(load_state(root), args.project)
            result = {'id': 'r-' + uuid.uuid4().hex[:12], 'title': args.title, 'content': args.content, 'category': args.category, 'scope': 'project' if args.project else 'global', 'projectId': args.project or '', 'enabled': True}
            mutate(root, lambda s: s['rules'].append(result))
    elif args.command == 'prompt':
        print(prompt_text(load_state(root), args.id, args.project)); return 0
    elif args.command == 'guide':
        s = load_state(root)
        result = guide_refresh(root, s['config']) if args.action == 'refresh' else (read_json(root / 'guides/manifest.json') if (root / 'guides/manifest.json').exists() else {'status': 'not_cached', 'officialUrl': s['config']['guideUrl']})
    elif args.command == 'import-state':
        incoming = read_json(args.file); validate_state(incoming)
        def change(s):
            if s['revision'] != args.expected_revision: raise WorkflowError('磁盘版本已变化，请先重新读取；不覆盖并发修改')
            incoming['workspaceId'] = s['workspaceId']; incoming['workspaceRoot'] = str(root); incoming['revision'] = s['revision']
            s.clear(); s.update(incoming)
        mutate(root, change); result = {'imported': True}
    else: raise WorkflowError('未知命令')
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (WorkflowError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as e:
        print(json.dumps({'error': str(e) if isinstance(e, WorkflowError) else type(e).__name__}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
