#!/usr/bin/env python3
"""Build a browser-readable bundle from the same templates and Skill sources."""
from pathlib import Path
import argparse
import json

args=argparse.ArgumentParser()
args.add_argument('--check',action='store_true')
options=args.parse_args()
root = Path(__file__).resolve().parents[1]
bundle = {'version': '0.1.0', 'defaults': json.loads((root/'templates/default-state.json').read_text(encoding='utf-8')),
          'prompts': json.loads((root/'templates/prompts.json').read_text(encoding='utf-8')),
          'handbook': [{'title': title, 'body': (root/path).read_text(encoding='utf-8')} for title,path in [
              ('Skill 说明','SKILL.md'),('安装与启动','references/quickstart.md'),
              ('工作流配置','references/comfyui.md'),('H3 提示词','references/h3-prompts.md'),
              ('制作与审片','references/production.md')]]}
target=root/'assets/workstation/bundle.js'
content='window.MW_BUNDLE = '+json.dumps(bundle,ensure_ascii=False).replace('<','\\u003c')+';\n'
if options.check:
    if target.read_text(encoding='utf-8') != content:
        raise SystemExit('Workstation bundle is stale; run scripts/build_bundle.py')
    print('Workstation bundle matches canonical sources')
else:
    target.write_text(content,encoding='utf-8')
    print('Built workstation bundle from canonical Skill and template files')
