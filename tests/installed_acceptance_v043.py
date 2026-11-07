"""Opt-in installed R6.4 journey, deliberately outside pytest discovery.

Operator configuration: USERPROFILE/.okto-pulse/test-fixtures/r6-installed-my-board.json.
Required keys are checked before any mutation. The configuration contains IDs,
file locations/hashes and UI text, never previous observations or receipts to
publish. A native runner must execute this journey; reading its journal is not
an alternative acceptance test. No DB access, Source updates or automatic retry.

Configuration contract (all paths absolute):
  contract_version='r6-installed-my-board/v1', run_id, output_directory, base_url,
  playwright_cwd, playwright_command (rtk proxy ... playwright-cli),
  request_timeout_seconds, readiness_timeout_seconds, browser_timeout_seconds,
  spec_title, scenario_sha256 (native raw digest), binding_semantics {ref: digest},
  bases {A,B}: normal_card_id, test_card_id, target_id, target_revision,
    execution_id, implementation_id, title, test_title, revision,
    source_sha256 (Git Target blob), contribution_scopes {ref: digest};
  provenance: package_plan {path,sha256}, frontend_origin_plan {path,sha256},
    repository, runtime {pid,created_at,
    executable,site_packages}, frontend_input_paths;
  human: card_id, update_content, update_remaining, delivery_content,
    delivery_remaining.
The existing QA Board/Spec/edition/scenario/Source and A/B commits are fixed below.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


BOARD = '1cf87df4-e744-4d24-b988-801914fc7a0c'
SPEC = '3a1eec3a-9c95-42d4-a16b-6b1e4e344ef5'
EDITION = 3
SCENARIO = 'ts_qa_v043'
SOURCE = 'source_2dc8e5f0f50a4ebe8649c8df486a125a'
NORMAL = '345fd902-1446-574b-9472-5b3c987ca11f'
TEST = 'fc97baae-0163-5747-ba78-8c3900e809d5'
A_REV = '670067eb3d5dde1ce7f24cc83a81d31225dc9320'
B_REV = '876e57c4dc84e06c5227ef67e16193524dc6a1e9'
TARGET_PATH = 'frontend/src/components/specs/SpecPlanningSummary.tsx'
REFS = ('ac:ac_qa_v043', 'br:br_qa_v043', 'fr:fr_qa_v043')
ASSOCIATED = {'A': REFS, 'B': ('br:br_qa_v043',)}
MONITORED = {'A': ('ac:ac_qa_v043', 'fr:fr_qa_v043'), 'B': ('br:br_qa_v043',)}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def now():
    return datetime.now(timezone.utc).isoformat()


def file_reference(path):
    path = Path(path).resolve()
    return {'reference': path.as_posix(), 'revision': '1', 'sha256': digest(path.read_bytes())}


def absolute(value):
    path = Path(value)
    require(path.is_absolute(), 'Operator configuration requires absolute paths')
    return path


def load_configuration():
    profile = os.environ.get('USERPROFILE')
    require(profile, 'USERPROFILE is required; no implicit installed fixture')
    path = Path(profile) / '.okto-pulse/test-fixtures/r6-installed-my-board.json'
    require(path.is_file(), f'Explicit operator fixture configuration missing: {path}')
    raw = path.read_bytes()
    config = json.loads(raw)
    required = {'contract_version', 'run_id', 'output_directory', 'base_url',
        'playwright_cwd', 'playwright_command', 'request_timeout_seconds',
        'readiness_timeout_seconds', 'browser_timeout_seconds', 'spec_title',
        'scenario_sha256', 'binding_semantics', 'bases', 'provenance', 'human'}
    require(set(config) == required, 'Configuration fields do not match the installed fixture contract')
    require(config['contract_version'] == 'r6-installed-my-board/v1', 'Unsupported fixture configuration')
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', config['run_id']), 'Explicit unique run_id required')
    require(re.fullmatch(r'[0-9a-f]{64}', config['scenario_sha256']), 'Native scenario digest required')
    require(set(config['binding_semantics']) == set(REFS), 'Exact three existing QA bindings required')
    require(all(re.fullmatch(r'[0-9a-f]{64}', v) for v in config['binding_semantics'].values()),
            'Native binding semantic hashes required')
    require(set(config['bases']) == {'A', 'B'}, 'Two explicit immutable bases required')
    for name, revision in [('A', A_REV), ('B', B_REV)]:
        base = config['bases'][name]
        require(set(base) == {'normal_card_id', 'test_card_id', 'target_id', 'target_revision',
            'execution_id', 'implementation_id', 'title', 'test_title', 'revision',
            'source_sha256', 'contribution_scopes'}, f'Incomplete {name} native fixture mapping')
        require(base['revision'] == revision, f'Unexpected {name} immutable revision')
        require(base['normal_card_id'] == NORMAL and base['test_card_id'] == TEST,
                'Only the existing QA Normal/Test fixture is authorized')
        require(all(isinstance(base[k], str) and base[k].strip() for k in
            ('normal_card_id', 'test_card_id', 'target_id', 'execution_id', 'implementation_id', 'title', 'test_title')),
            f'Missing {name} identities; placeholders are not a fixture')
        require(set(base['contribution_scopes']) == set(ASSOCIATED[name]), 'Exact native contribution scopes required')
        require(all(re.fullmatch(r'[0-9a-f]{64}', v) for v in base['contribution_scopes'].values()),
                'Native contribution scope hashes required')
        require(re.fullmatch(r'[0-9a-f]{64}', base['source_sha256']), 'Actual Target Git blob hash required')
    require(config['bases']['A']['target_id'] != config['bases']['B']['target_id'], 'A and B require distinct Targets')
    require(config['bases']['A']['execution_id'] != config['bases']['B']['execution_id'], 'Distinct executions required')
    require(config['bases']['A']['implementation_id'] != config['bases']['B']['implementation_id'], 'Distinct implementation records required')
    require(config['human']['card_id'] in {b['normal_card_id'] for b in config['bases'].values()}, 'Human submission must belong to the fixture')
    require(set(config['human']) == {'card_id', 'update_content', 'update_remaining',
        'delivery_content', 'delivery_remaining'}, 'Explicit two human form intents required')
    require(all(isinstance(v, str) for v in config['human'].values()) and
            all(config['human'][k].strip() for k in ('update_content', 'delivery_content')), 'Human content is required')
    for key in ('request_timeout_seconds', 'readiness_timeout_seconds', 'browser_timeout_seconds'):
        require(type(config[key]) is int and 1 <= config[key] <= 600, f'Explicit bounded {key} required')
    absolute(config['output_directory'])
    require(absolute(config['playwright_cwd']).is_dir(), 'Operator Playwright cwd missing')
    command = config['playwright_command']
    require(isinstance(command, list) and all(isinstance(s, str) for s in command)
        and command[:2] == ['rtk', 'proxy'] and command[-1] == 'playwright-cli'
        and not any(s in command for s in ('run-code', '--session')), 'Explicit RTK Playwright CLI command required')
    require(shutil.which('npx') or shutil.which('npx.cmd'), 'npx is unavailable')
    return config, path, raw


class Journal:
    def __init__(self, config, path, raw):
        self.root = absolute(config['output_directory']) / config['run_id']
        self.root.mkdir(parents=True, exist_ok=False)
        self.sequence = 0
        self.save('operator-config.json', config)
        self.save('run.json', {'started_at': now(), 'config_path': str(path),
            'config_sha256': digest(raw), 'automatic_retry': False,
            'board_id': BOARD, 'spec_id': SPEC, 'spec_edition': EDITION, 'scenario_id': SCENARIO})

    def save(self, name, value):
        path = self.root / name
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        return path

    def stem(self, label):
        self.sequence += 1
        return f'{self.sequence:04d}-{label}'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Rest:
    def __init__(self, config, journal):
        parsed = urllib.parse.urlsplit(config['base_url'])
        require(parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in {'', '/'}, 'Explicit loopback Community URL required')
        self.base = config['base_url'].rstrip('/')
        self.config = config
        self.timeout = config['request_timeout_seconds']
        self.journal = journal
        self.opener = urllib.request.build_opener(NoRedirect())
        self.stopped = False

    def request(self, label, path, method='GET', body=None, binary=False):
        require(not self.stopped, 'REST stopped after an uncertain/error response; do not retry')
        require(path.startswith('/') and not path.startswith('//'), 'Relative installed route required')
        if method != 'GET':
            allowed = {f'/api/v1/specs/{SPEC}/scenarios/{SCENARIO}/evidence/reports',
                f'/api/v1/specs/{SPEC}/scenarios/{SCENARIO}/status'}
            allowed.update(f'/api/v1/boards/{BOARD}/cards/{b["test_card_id"]}/specs/{SPEC}/delivery-evidence'
                           for b in self.config['bases'].values())
            require(path in allowed and method in {'POST', 'PATCH'}, 'Mutation is outside the prepared fixture')
        stem = self.journal.stem(label)
        self.journal.save(stem + '-request.json', {'method': method, 'path': path, 'body': body})
        request = urllib.request.Request(self.base + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                self.journal.save(stem + '-response.json', {'status': response.status,
                    'terminal_response': True, 'sha256': digest(raw),
                    'body': None if binary else raw.decode('utf-8')})
                if binary:
                    return raw
                value = json.loads(raw)
                if isinstance(value, dict) and 'outcome' in value:
                    require(value['outcome'] == 'success', 'Native REST returned a non-success outcome')
                    return value['data']
                return value
        except Exception as exc:
            self.stopped = True
            details = {'state': 'unknown_stop', 'exception_type': type(exc).__name__, 'automatic_retry': False}
            if isinstance(exc, urllib.error.HTTPError):
                details.update(state='http_error_stop', status=exc.code,
                    body=exc.read().decode('utf-8', errors='replace'), execution_terminality='not_inferred')
            self.journal.save(stem + '-error.json', details)
            raise

    def delivery(self, label, **query):
        query = {'limit': 20, **query}
        return self.request(label, f'/api/v1/boards/{BOARD}/specs/{SPEC}/delivery-evidence?'
                            + urllib.parse.urlencode(query))


def git_bytes(repository, *args):
    return subprocess.check_output(['rtk', 'proxy', 'git', '-C', str(repository), *args], stderr=subprocess.PIPE)


def verify_provenance(config, rest, journal):
    """Recheck real package contents, process and served bytes, never /health identity."""
    import psutil
    provenance = config['provenance']
    plan_ref = provenance['package_plan']
    plan_raw = absolute(plan_ref['path']).read_bytes()
    require(digest(plan_raw) == plan_ref['sha256'], 'Package source plan changed')
    plan = json.loads(plan_raw)
    origin_ref = provenance['frontend_origin_plan']
    origin_raw = absolute(origin_ref['path']).read_bytes()
    require(digest(origin_raw) == origin_ref['sha256'], 'Original A/B frontend provenance changed')
    origin = json.loads(origin_raw)
    reuse = origin['frontend_reuse']
    require(reuse['from_revision'] == A_REV
        and reuse['to_revision'] == B_REV and reuse['changed_frontend_paths'] == []
        and reuse['file_count'] == 79 and len(reuse['files']) == 79, 'Exact A frontend -> B wheel provenance required')
    installed_revision = plan['community_revision']
    require(re.fullmatch(r'[0-9a-f]{40}', installed_revision), 'Actual installed source revision required')
    require(plan['frontend_reuse']['files'] == reuse['files'], 'Final wheel frontend differs from original A/B chain')
    repository = absolute(provenance['repository'])
    inputs = provenance['frontend_input_paths']
    require(inputs == reuse['diff_paths'], 'All recorded frontend build inputs must be checked')
    require(not git_bytes(repository, 'diff', '--name-only', A_REV, B_REV, '--', *inputs).strip(),
            'Frontend build inputs differ between A and B')
    require(not git_bytes(repository, 'diff', '--name-only', B_REV, installed_revision, '--', *inputs).strip(),
            'Frontend build inputs differ in the final installed source')
    for name, revision in [('A', A_REV), ('B', B_REV)]:
        require(digest(git_bytes(repository, 'show', f'{revision}:{TARGET_PATH}'))
                == config['bases'][name]['source_sha256'], f'{name} Target source is not its real Git blob')
    previous = absolute(reuse['verified_report'])
    require(digest(previous.read_bytes()) == reuse['verified_report_sha256'], 'Original frontend wheel verification changed')
    verified = json.loads(previous.read_bytes())
    require(verified['result'] == 'passed' and any(o.get('revision') == A_REV for o in verified['observations']),
            'Original A build verification lacks the actual revision')
    runtime = provenance['runtime']
    process = psutil.Process(runtime['pid'])
    require(abs(process.create_time() - runtime['created_at']) < 0.01
            and Path(process.exe()).resolve() == absolute(runtime['executable']).resolve(), 'Installed process identity changed')
    address = urllib.parse.urlsplit(config['base_url'])
    port = address.port or 80
    # Windows can expose an owned listening socket with status NONE. Prove
    # ownership through the exact live health connection instead of inferring
    # that the absence of CONN_LISTEN means a foreign server owns this port.
    connection = http.client.HTTPConnection(address.hostname, port, timeout=10)
    try:
        connection.connect()
        client_endpoint = tuple(connection.sock.getsockname()[:2])
        connection.request('GET', '/health')
        response = connection.getresponse()
        health = json.loads(response.read())
        require(response.status == 200 and health.get('status') == 'healthy'
                and health.get('version') == plan['version'], 'Installed health request failed')
        require(any(c.status == psutil.CONN_ESTABLISHED and c.laddr.port == port
                    and tuple(c.raddr) == client_endpoint
                    for c in process.net_connections(kind='tcp')),
                'Configured runtime does not own the verified health connection')
        ownership = {'method': 'established_health_connection', 'pid': process.pid,
                     'server_port': port, 'client_endpoint': client_endpoint, 'health': health}
    finally:
        connection.close()
    site = absolute(runtime['site_packages'])
    wheel_frontend = {}
    for name, wheel in plan['wheels'].items():
        raw = absolute(wheel['path']).read_bytes()
        require(digest(raw) == wheel['sha256'], f'{name} wheel changed')
        with zipfile.ZipFile(wheel['path']) as archive:
            members = [n for n in archive.namelist() if n.startswith('okto_pulse/') and not n.endswith('/')]
            require(members, 'Wheel has no package files')
            for member in members:
                actual = site / member
                content = archive.read(member)
                require(actual.is_file() and actual.read_bytes() == content, f'Installed member differs: {member}')
                require(actual.stat().st_mtime <= process.create_time() + 1, 'Package changed after runtime startup')
                prefix = 'okto_pulse/community/frontend_dist/'
                if member.startswith(prefix):
                    wheel_frontend[member[len(prefix):]] = digest(content)
    require(wheel_frontend == reuse['files'], 'B wheel does not contain the exact 79-file A frontend')
    source_dir = absolute(reuse['source_directory'])
    local_files = {p.relative_to(source_dir).as_posix(): digest(p.read_bytes())
                   for p in source_dir.rglob('*') if p.is_file()}
    require(local_files == wheel_frontend, 'Original frontend build changed or population differs')
    served = {}
    for relative, expected in wheel_frontend.items():
        if relative.startswith('assets/'):
            raw = rest.request('served-asset', '/' + relative, binary=True)
            require(digest(raw) == expected, f'Served asset differs: {relative}')
            served['/' + relative] = expected
    html = rest.request('served-index', '/', binary=True).decode('utf-8')
    expected_html = (source_dir / 'index.html').read_text(encoding='utf-8').replace(
        '</head>', '  <script src="/config.js"></script>\n</head>')
    require(html == expected_html, 'Served index differs from the exact build and native config injection')
    module_scripts = re.findall(r'<script[^>]+type="module"[^>]+src="([^"]+)"', html)
    require(module_scripts and all(path in served for path in module_scripts), 'Served index selects an unverified script')
    observation = {'observed_at': now(), 'runtime': runtime, 'connection_ownership': ownership,
        'package_plan_sha256': digest(plan_raw),
        'frontend_origin_revision': A_REV, 'fixture_B_revision': B_REV,
        'installed_community_revision': installed_revision,
        'installed_core_revision': plan['core_revision'], 'frontend_file_count': 79,
        'served_assets': served, 'module_scripts': module_scripts,
        'served_index_matches_native_config_injection': True,
        'claim': 'Exact frontend artifact A is served by the separately identified installed backend. Neither historical backend A nor B is claimed executed.'}
    journal.save(journal.stem('provenance') + '.json', observation)
    return observation


class Browser:
    """Existing operator session only. CLI errors never trigger a second action."""
    def __init__(self, config, journal):
        self.config, self.journal = config, journal
        self.command = [*config['playwright_command'], '--session', 'human-delivery']
        self.stopped = False

    def call(self, label, script, data):
        require(not self.stopped, 'Browser stopped; do not retry an uncertain UI action')
        stem = self.journal.stem(label)
        script_path = self.journal.root / (stem + '.js')
        script_path.write_text('async (page) => {\nconst c = ' + json.dumps(data, ensure_ascii=True)
                              + ';\n' + script + '\n}', encoding='utf-8')
        try:
            # Refresh the observed UI before every explicit multi-step script.
            snapshot = subprocess.run([*self.command, 'snapshot'], cwd=self.config['playwright_cwd'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=self.config['browser_timeout_seconds'], check=False)
            (self.journal.root / (stem + '-snapshot.log')).write_text(snapshot.stdout + snapshot.stderr, encoding='utf-8')
            require(snapshot.returncode == 0, 'Playwright session snapshot failed')
            result = subprocess.run([*self.command, 'run-code', '--filename', str(script_path)],
                cwd=self.config['playwright_cwd'], capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=self.config['browser_timeout_seconds'], check=False)
            (self.journal.root / (stem + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
            require(result.returncode == 0 and '### Result\n' in result.stdout, 'Browser result uncertain or failed')
            value = json.loads(result.stdout.split('### Result\n', 1)[1].split('\n### ', 1)[0].strip())
            self.journal.save(stem + '.json', value)
            return value, script_path, self.journal.root / (stem + '.json')
        except Exception as exc:
            self.stopped = True
            self.journal.save(stem + '-stopped.json', {'state': 'unknown_stop',
                'exception_type': type(exc).__name__, 'automatic_retry': False,
                'browser_action_may_have_completed': True})
            raise

    def board(self):
        self.call('open-my-board', """
await page.goto(c.base_url);
await page.getByRole('button',{name:'My Board',exact:true}).click();
await page.getByRole('button',{name:'Tasks',exact:true}).waitFor({state:'visible'});
return {observed_at:new Date().toISOString(),url:page.url(),board:'My Board'};
""", {'base_url': self.config['base_url']})

    def task(self, base):
        self.call('open-task', """
const closeSpec=page.getByRole('button',{name:'Close spec',exact:true});
if(await closeSpec.count()) await closeSpec.click();
// Returning to the known Board avoids guessing an unobserved modal close control.
await page.goto(c.base_url);
await page.getByRole('button',{name:'My Board',exact:true}).click();
await page.getByRole('button',{name:'Tasks',exact:true}).click();
await page.getByRole('textbox',{name:'Search cards…',exact:true}).fill(c.title);
const card=page.getByRole('button').filter({has:page.getByRole('heading',{name:c.title,level:4,exact:true})});
await card.waitFor({state:'visible'});
if(await card.count()!==1) throw Error('Fixture Card title is absent or ambiguous');
await card.getByRole('button',{name:'Open delivery details',exact:true}).click();
await page.getByRole('region',{name:'Task delivery',exact:true}).waitFor({state:'visible'});
return {observed_at:new Date().toISOString(),title:await page.getByRole('heading',{name:c.title,level:2,exact:true}).innerText()};
""", {'base_url': self.config['base_url'], 'title': base['title']})

    def matrix(self, label, base, expected):
        self.task(base)
        image_path = self.journal.root / (label + '-matrix.png')
        return self.call(label + '-matrix', """
await page.getByRole('button',{name:'Refresh card',exact:true}).click();
const delivery=page.getByRole('region',{name:'Task delivery',exact:true});
const table=delivery.getByRole('table',{name:'Implementation and verification by requirement',exact:true});
await table.waitFor({state:'visible'});
const label={satisfied:'Verified',missing:'Pending',partial:'Partial',failed:'Failed',stale:'Needs recheck',conflict:'Needs review'};
// Wait for presentation to agree with the already current native snapshot.
await page.waitForFunction(({expected,label})=>{
 const t=document.querySelector('table[aria-label="Implementation and verification by requirement"]');
 if(!t)return false;
 const rows=[...t.querySelectorAll('tbody tr')];
 return rows.length===expected.length && expected.every(e=>rows.some(r=>{
  const th=r.querySelector('th'),cells=r.querySelectorAll('td');
  return th?.textContent.includes(e.title)&&cells.length===2&&cells[0].textContent.includes('Implemented')&&cells[1].textContent.includes(label[e.state]);
 }));
},{expected:c.expected,label},{timeout:c.timeout});
const rows=await table.locator('tbody tr').evaluateAll(rs=>rs.map(r=>({title:r.querySelector('th')?.textContent,cells:[...r.querySelectorAll('td')].map(e=>e.textContent)})));
if(await delivery.getByRole('button',{name:'Show details',exact:true}).count()) await delivery.getByRole('button',{name:'Show details',exact:true}).click();
const detail=delivery.getByRole('table',{name:'Requirements and verification',exact:true});
await detail.waitFor({state:'visible'});
const pages=[];
for(let i=0;i<50;i++){
 const text=await detail.innerText();if(pages.includes(text))throw Error('Details pagination did not advance');pages.push(text);
 const next=delivery.getByRole('button',{name:'Next checks',exact:true});
 if(!await next.count()||!await next.isEnabled())break;
 if(i===49)throw Error('Unbounded details pagination');
 await next.click();await detail.waitFor({state:'visible'});
 await page.waitForFunction(old=>document.querySelector('table[aria-label="Requirements and verification"]')?.innerText!==old,text);
}
await delivery.scrollIntoViewIfNeeded();await page.screenshot({path:c.screenshot,animations:'disabled'});
return {observed_at:new Date().toISOString(),rows,detail_pages:pages,summary:await delivery.innerText(),screenshot:c.screenshot};
""", {'expected': expected, 'timeout': self.config['readiness_timeout_seconds'] * 1000, 'screenshot': str(image_path)})[0]

    def observe_icon(self, label, failed):
        screenshot = str(self.journal.root / (label + '-icon.png'))
        return self.call(label + '-icon', """
await page.goto(c.base_url);
await page.getByRole('button',{name:'My Board',exact:true}).click();
await page.getByRole('heading',{name:c.spec_title,exact:true}).click();
await page.getByRole('tablist',{name:'Spec sections',exact:true}).getByRole('tab',{name:'Validation',exact:true}).click();
const region=page.getByRole('region',{name:'Validation result',exact:true});
await region.waitFor({state:'visible'});
const icon=region.getByRole('img');
await icon.waitFor({state:'visible'});
if(await icon.count()!==1)throw Error('Exactly one native validation icon required');
const name=await icon.getAttribute('aria-label'),text=await region.innerText();
if(!['Validation succeeded','Validation not complete'].includes(name)||!text.trim())throw Error('QA verdict unavailable or outside check/X fixture');
if(name==='Validation succeeded'&&!text.includes('Spec validated'))throw Error('Check icon and native text disagree');
if(name==='Validation not complete'&&!text.includes('Spec not yet validated')&&!text.includes('Ready to conclude validation'))throw Error('X icon and native text disagree');
const before={visible:await icon.isVisible(),name,text};let style,observed;
try{
 if(c.failed){
  style=await page.addStyleTag({content:'[data-testid="spec-validation-summary"] [role="img"] { visibility: hidden !important; }'});
  await icon.waitFor({state:'hidden'});
 }
 observed={observed_at:new Date().toISOString(),visible:await icon.isVisible(),name,text:await region.innerText()};
 if(observed.visible===c.failed)throw Error('Actual UI outcome differs from requested observation');
 await page.screenshot({path:c.screenshot,animations:'disabled'});
}finally{if(style)await style.evaluate(el=>el.remove());}
await icon.waitFor({state:'visible'});
return {observed_at:new Date().toISOString(),before,observed,restored:await icon.isVisible(),
 mode:c.failed?'Intentional browser-local CSS fault, not a production defect':'Fresh direct observation',
 asset:await page.locator('script[type="module"]').getAttribute('src'),screenshot:c.screenshot};
""", {'base_url': self.config['base_url'], 'spec_title': self.config['spec_title'],
       'failed': failed, 'screenshot': screenshot})

    def human(self, action, base):
        self.task(base)
        human = self.config['human']
        kind = 'update' if action == 'progress' else 'delivery'
        return self.call('human-' + kind, """
await page.getByRole('button',{name:'Refresh card',exact:true}).click();
await page.getByRole('button',{name:c.action==='progress'?'Record an update':'Submit delivery',exact:true}).click();
const form=page.getByRole('form',{name:c.action==='progress'?'Task update':'Delivery report',exact:true});
if(await form.getByRole('textbox').count()!==2)throw Error('Human form asks for unexpected technical fields');
await form.getByRole('textbox',{name:'What was delivered or changed?',exact:true}).fill(c.content);
await form.getByRole('textbox',{name:'What remains? (optional)',exact:true}).fill(c.remaining);
const [response]=await Promise.all([
 page.waitForResponse(r=>new URL(r.url()).pathname===c.route&&r.request().method()==='POST'),
 form.getByRole('button',{name:c.action==='progress'?'Save update':'Submit for validation',exact:true}).click()
]);
const body=await response.json(),request=response.request().postDataJSON();
if(response.status()!==201||!body.receipt_id||!body.submission_id||body.replayed||Object.keys(request).some(k=>/actor|receipt/i.test(k)))throw Error('Automatic native human receipt not obtained');
if(request.action!==c.action||request.content!==c.content||request.expected_spec_edition!==3)throw Error('Wrong human submission intent');
await page.getByRole('button',{name:'Refresh card',exact:true}).click();
await page.getByRole('region',{name:'Task delivery',exact:true}).getByText(c.content,{exact:true}).waitFor({state:'visible'});
await page.screenshot({path:c.screenshot,animations:'disabled'});
return {observed_at:new Date().toISOString(),status:response.status(),request,result:body,screenshot:c.screenshot};
""", {'action': action, 'content': human[kind + '_content'], 'remaining': human[kind + '_remaining'],
       'route': f'/api/v1/boards/{BOARD}/cards/{human["card_id"]}/human-submissions',
       'screenshot': str(self.journal.root / ('human-' + kind + '.png'))})[0]


class InstalledJourney:
    def __init__(self, config, journal):
        self.c, self.j = config, journal
        self.rest, self.browser = Rest(config, journal), Browser(config, journal)
        self.issued, self.associations = {}, {}
        self.last_ordinals = {}
        self.scenario_path = f'/api/v1/specs/{SPEC}/scenarios/{SCENARIO}'
        self.card_ids = sorted({b[k] for b in config['bases'].values() for k in ('normal_card_id', 'test_card_id')})

    def spec(self):
        value = self.rest.request('qa-spec', f'/api/v1/specs/{SPEC}')
        require(value['id'] == SPEC and value['board_id'] == BOARD and value['edition'] == EDITION
            and value['status'] == 'in_progress', 'Existing QA edition/status changed')
        scenario = [s for s in value['test_scenarios'] if s['id'] == SCENARIO]
        require(len(scenario) == 1 and scenario[0]['verification_method'] == 'demonstration', 'QA demonstration contract changed')
        from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
        actual = compute_test_scenario_semantic_sha256(board_id=BOARD, spec_id=SPEC,
            scenario=scenario[0], acceptance_criteria=value['acceptance_criteria']).removeprefix('sha256:')
        require(actual == self.c['scenario_sha256'], 'Native scenario semantic contract changed')
        return value

    def ledger(self, card_id, edition=EDITION):
        items, cursor, seen, total = {}, None, set(), None
        while True:
            query = dict(view='ledger', card_id=card_id, edition=edition)
            if cursor:
                query['cursor'] = cursor
            page = self.rest.delivery('ledger-page', **query)
            require((page['board_id'], page['card_id'], page['spec_id'], page['edition'])
                == (BOARD, card_id, SPEC, edition), 'Ledger scope changed')
            total = page['total'] if total is None else total
            require(total == page['total'] and total <= 2000, 'Ledger population changed or exceeds bounded fixture')
            for entry in page['items']:
                require(entry['id'] not in items, 'Duplicate ledger record')
                detail = self.rest.delivery('ledger-record', view='ledger', card_id=card_id,
                    edition=edition, record_id=entry['id'])
                require(detail['detail'] and len(detail['items']) == 1, 'Full native record detail missing')
                row = detail['items'][0]
                require(row['id'] == entry['id'] and not row['text_truncated'], 'Record detail incomplete')
                items[row['id']] = row
            cursor = page['next_cursor']
            if cursor is None:
                break
            require(cursor not in seen and len(seen) < 100, 'Invalid ledger pagination')
            seen.add(cursor)
        require(len(items) == total, 'Incomplete ledger population')
        return items

    def history(self):
        records = {}
        for card in self.card_ids:
            for edition in range(1, EDITION + 1):
                for identity, record in self.ledger(card, edition).items():
                    # can_correct depends on current workflow, unlike the historical declaration.
                    stable = {k: v for k, v in record.items() if k != 'can_correct'}
                    records[(card, edition, identity)] = digest(canonical(stable))
        return records

    def proofs(self):
        require(self.spec(), 'QA Spec unavailable')
        result = {}
        for card in sorted({b['normal_card_id'] for b in self.c['bases'].values()}):
            resume = self.rest.delivery('normal-resume', view='resume', card_id=card)
            require(not resume['response_truncated'] and not resume['obligations']['truncated']
                and resume['obligations']['total'] == len(resume['obligations']['items'])
                and not resume['implementation_proofs']['truncated'] and not resume['targets']['truncated'],
                'Native proof manifest incomplete')
            require(resume['edition'] == EDITION and resume['spec_id'] == SPEC and resume['board_id'] == BOARD,
                    'Native resume scope changed')
            ledger = self.ledger(card)
            for name, base in self.c['bases'].items():
                if base['normal_card_id'] != card:
                    continue
                proof = [p for p in resume['implementation_proofs']['items'] if p['record_id'] == base['implementation_id']]
                target = [p for p in resume['targets']['items'] if p['id'] == base['target_id']]
                require(len(proof) == len(target) == 1, f'{name} native Target/proof missing')
                proof, target = proof[0], target[0]
                require(not proof['executions_truncated'] and not proof['bindings_truncated']
                    and set(proof['current_obligation_refs']) == set(ASSOCIATED[name]), 'Current implementation bindings changed')
                require(target['revision'] == base['target_revision'] and target['source_ref'] == SOURCE
                    and target['relative_path'] == TARGET_PATH, 'Immutable Target fence changed')
                executions = proof['executions']
                require(len(executions) == 1 and executions[0] == {
                    'execution_id': base['execution_id'], 'source_ref': SOURCE,
                    'result_revision': base['revision'], 'relative_path': TARGET_PATH}, 'Actual immutable execution mapping changed')
                record = ledger[base['implementation_id']]
                require(not record['revoked'], 'Implementation was revoked')
                payload = record['payload']
                require({b['obligation_ref']: b['semantic_sha256'] for b in payload['bindings']}
                    == {r: self.c['binding_semantics'][r] for r in ASSOCIATED[name]}, 'Native binding semantics changed')
                require({b['obligation_ref']: b['scope_sha256'] for b in payload['contribution_scopes']}
                    == base['contribution_scopes'], 'Native contribution scope changed')
                require({p['obligation_ref']: (p['contribution'], p['execution_ids']) for p in payload['contributions']}
                    == {r: ('complete', [base['execution_id']]) for r in ASSOCIATED[name]}, 'Declared contribution/execution changed')
                # Revalidate current admission on every call, but compare only
                # immutable identities/content across the long browser journey.
                # Resume freshness/TTL presentation is not historical content.
                result[name] = {
                    'target': {key: target[key] for key in ('id', 'revision', 'source_ref', 'relative_path')},
                    'executions': executions,
                    'binding_semantics': {r: self.c['binding_semantics'][r] for r in ASSOCIATED[name]},
                    'contribution_scopes': base['contribution_scopes'],
                    'record_sha256': digest(canonical(payload)),
                }
        require(set(result) == {'A', 'B'}, 'Both canonical native proofs are required')
        return result

    def projection(self, card_id=None):
        endpoint = f'/api/v1/boards/{BOARD}/specs/{SPEC}/delivery-readiness-projection'
        deadline, cursor, units, generation = time.monotonic() + self.c['readiness_timeout_seconds'], None, [], None
        while True:
            query = {'unit_limit': 250}
            if card_id is not None:
                query['card_id'] = card_id
            if cursor:
                query['cursor'] = cursor
            page = self.rest.request('current-projection', endpoint + '?' + urllib.parse.urlencode(query))
            p = page['projection']
            if p['freshness'] != 'current':
                require(cursor is None and p['freshness'] in {'updating', 'pending_initial'}
                    and time.monotonic() < deadline, 'Current installed projection unavailable')
                time.sleep(0.5)  # Only reads wait; no mutation is retried.
                continue
            require(p['source_revision'] == p['target_revision'] and p['generation_id'], 'Incoherent readiness generation')
            require(page['scope']['board_id'] == BOARD and page['scope']['spec_id'] == SPEC
                and page['scope']['spec_edition'] == EDITION and page['scope']['card_id'] == card_id, 'Readiness scope changed')
            fence = (p['generation_id'], p['source_revision'], page['scope']['spec_version'])
            generation = generation or fence
            require(fence == generation, 'Readiness pagination mixed generations')
            require(all(s['population_known'] and s['evaluation_complete'] for s in page['summaries']), 'Unknown readiness population')
            units.extend(page['units'])
            if not page['page']['has_more']:
                require(len(units) == page['page']['total'], 'Readiness units incomplete')
                break
            next_cursor = page['page']['next_cursor']
            require(next_cursor and next_cursor != cursor and len(units) < 5000, 'Readiness cursor did not advance')
            cursor = next_cursor
        keys = [(u['card_id'], u['kind'], u['ref'], u['semantic_sha256']) for u in units]
        require(len(keys) == len(set(keys)), 'Duplicate readiness unit')
        page['units'] = units
        return page

    def checkpoint(self, label, a, b):
        before = self.projection()
        self.proofs()
        spec = self.spec()
        titles = {}
        for family, field in [('ac', 'acceptance_criteria'), ('fr', 'functional_requirements'), ('br', 'business_rules')]:
            for item in spec[field]:
                ref = family + ':' + item['id']
                if ref in REFS:
                    titles[ref] = item.get('title') or item.get('text') or item.get('description') or item.get('criterion')
        require(set(titles) == set(REFS) and all(titles.values()), 'Exact QA requirement titles unavailable')
        expected = {ref: a for ref in MONITORED['A']} | {ref: b for ref in MONITORED['B']}
        scopes = {None, *(base['normal_card_id'] for base in self.c['bases'].values())}
        scoped = {None: before}
        for card in sorted(scopes - {None}):
            scoped[card] = self.projection(card)
            require(scoped[card]['projection'] == before['projection'], 'Card/global readiness generations differ')
        selected = [u for page in scoped.values() for u in page['units'] if u['kind'] == 'obligation']
        for card in scopes:
            rows = [u for u in selected if u['card_id'] == card]
            refs = set(REFS) if card is None else set(r for n, base in self.c['bases'].items()
                if base['normal_card_id'] == card for r in ASSOCIATED[n])
            require({u['ref'] for u in rows} == refs, 'Exact monitored obligation population differs')
            for row in rows:
                require(row['semantic_sha256'] == self.c['binding_semantics'][row['ref']]
                    and row['implementation_state'] == 'satisfied', 'Native obligation implementation/scope differs')
                state = row['verification_state']
                require(state in {'satisfied', 'missing', 'partial', 'failed', 'stale'}, 'Unknown/conflicting verification cannot pass')
                require((state == 'satisfied') == expected[row['ref']], f'{label}: incorrect verification of {row["ref"]}')
                if label == 'b_failed' and row['ref'] == 'br:br_qa_v043':
                    require(state == 'failed', 'Later B failure was not diagnosed as failure')
        for record_id, evidence in self.associations.items():
            base = self.c['bases'][evidence['base']]
            actual = self.ledger(base['test_card_id'])[record_id]
            payload = actual['payload']
            require(not actual['revoked'] and payload['accepted_result_id'] == evidence['accepted_result_id']
                and payload['test_receipt'] == evidence['receipt']
                and payload['implementation_ids'] == [base['implementation_id']]
                and payload['scenario_id'] == SCENARIO, 'Exact recorded receipt association changed')
        after = self.projection()
        require(before['projection'] == after['projection'], 'Readiness changed during checkpoint reads')
        for card in sorted(scopes - {None}):
            base = next(b for b in self.c['bases'].values() if b['normal_card_id'] == card)
            rows = [u for u in selected if u['card_id'] == card]
            self.browser.matrix(label + '-' + card, base,
                [{'title': titles[u['ref']], 'state': u['verification_state']} for u in rows])
        self.j.save(label + '-checkpoint.json', {'observed_at': now(), 'projection': after,
            'card_projections': {key: value for key, value in scoped.items() if key is not None},
            'expected_verified': expected, 'scope': {'board': BOARD, 'spec': SPEC, 'edition': EDITION}})

    def issue_publish(self, label, base_name, generation=None, failed=False):
        base = self.c['bases'][base_name]
        provenance = verify_provenance(self.c, self.rest, self.j)
        observation, script, observation_path = self.browser.observe_icon(label, failed)
        require(observation['before']['visible'] and observation['restored']
            and observation['observed']['visible'] is (not failed), 'No genuine fresh UI observation')
        require(observation['asset'] in provenance['served_assets'], 'Browser did not load verified frontend asset')
        environment = self.j.save(label + '-environment.json', {'observed_at': now(), 'provenance': provenance,
            'config_sha256': digest(canonical(self.c)), 'basis': base_name,
            'scope_limit': 'Frontend Target only; actual installed backend recorded separately.'})
        status = 'failed' if failed else 'passed'
        report = {'schema_version': 'verification-report/v1', 'method': 'demonstration',
            'report_id': self.c['run_id'] + '-' + label, 'observed_at': observation['observed']['observed_at'],
            'sources': [{'reference': SOURCE, 'revision': base['revision'], 'sha256': base['source_sha256']}],
            'procedure': file_reference(script), 'environment': file_reference(environment),
            'observations': [{'observation_id': label, 'criterion_id': 'ac_qa_v043',
                'observation_ref': observation_path.as_posix() + '#observed',
                'expected': 'The current-edition validation result has a visible graphical icon and matching text.',
                'observed': json.dumps(observation, ensure_ascii=False), 'outcome': status}],
            'conclusion': ('Actual failed browser observation under temporary CSS injection, restored in finally; not a product defect.'
                if failed else 'Fresh visible current-edition icon and native text observed on the verified installed frontend.'),
            'result': status}
        # Normalize only the native typed representation (e.g. ISO timestamp
        # precision), never the factual observation or its outcome.
        from okto_pulse.core.domain.verification_report import parse_verification_report
        report = parse_verification_report(report).model_dump(mode='json')
        self.j.save(label + '-report.json', report)
        response = self.rest.request(label + '-issue', self.scenario_path + '/evidence/reports', 'POST', {'report': report})
        evidence = response['evidence']
        context = evidence['verification_context']
        require(all(context[k] == v for k, v in {'board_id': BOARD, 'spec_id': SPEC, 'spec_edition': EDITION,
            'scenario_id': SCENARIO, 'verification_method': 'demonstration',
            'scenario_sha256': self.c['scenario_sha256']}.items()), 'Native issuance context mismatch')
        require(evidence['verification_report'] == report, 'Native receipt did not bind the exact fresh report')
        actual_generation = context['generation']
        require(generation is None or actual_generation == generation, 'Scenario generation changed unexpectedly')
        require(context['ordinal'] > self.last_ordinals.get(actual_generation, -1), 'Nonfresh native reservation')
        require(evidence['execution_receipt'] not in [v['receipt'] for v in self.issued.values()], 'Receipt was replayed')
        self.last_ordinals[actual_generation] = context['ordinal']
        published = self.rest.request(label + '-publish', self.scenario_path + '/status', 'PATCH',
                                      {'status': status, 'evidence': evidence})
        acceptance = published['result']['accepted_result_id']
        require(acceptance and acceptance not in [v['accepted_result_id'] for v in self.issued.values()], 'Fresh native acceptance missing')
        self.issued[label] = {'base': base_name, 'accepted_result_id': acceptance,
            'receipt': evidence['execution_receipt'], 'generation': actual_generation, 'status': status}
        return actual_generation

    def associate(self, label):
        issued = self.issued[label]
        base = self.c['bases'][issued['base']]
        resume = self.rest.delivery('test-cas', view='resume', card_id=base['test_card_id'])
        result = self.rest.request(label + '-associate',
            f'/api/v1/boards/{BOARD}/cards/{base["test_card_id"]}/specs/{SPEC}/delivery-evidence', 'POST', {
                'kind': 'test', 'expected_card_version': resume['card_version'], 'expected_spec_edition': EDITION,
                'idempotency_key': self.c['run_id'] + '-' + label, 'scenario_id': SCENARIO,
                'accepted_result_id': issued['accepted_result_id'], 'implementation_ids': [base['implementation_id']],
                'obligation_refs': list(ASSOCIATED[issued['base']]),
                'justification': 'R6.4 installed journey: bind this fresh native demonstration to its exact immutable implementation.'})
        require(result['id'] and result['replayed'] is False, 'New exact association was not recorded')
        self.associations[result['id']] = issued

    def human(self, action):
        base = next(b for b in self.c['bases'].values() if b['normal_card_id'] == self.c['human']['card_id'])
        observed = self.browser.human(action, base)
        result, request = observed['result'], observed['request']
        record = self.ledger(base['normal_card_id'])[result['submission_id']]
        receipt = record['payload']['human_submission']['receipt']
        from okto_pulse.core.models.human_task_submission import HumanTaskSubmissionCommand
        intent = HumanTaskSubmissionCommand(board_id=BOARD, card_id=base['normal_card_id'], spec_id=SPEC, **request)
        require(receipt['id'] == result['receipt_id'] and receipt['submission_id'] == result['submission_id']
            and receipt['board_id'] == BOARD and receipt['card_id'] == base['normal_card_id']
            and receipt['spec_id'] == SPEC and receipt['spec_edition'] == EDITION
            and receipt['actor_kind'] == 'human' and receipt['actor_id'] == record['actor_id']
            and receipt['action'] == action and receipt['content_sha256'] == intent.content_digest()
            and receipt['intent_sha256'] == intent.intent_digest(), 'Native automatic human receipt mismatched')
        require(record['payload']['justification'] == intent.content, 'Human content was not persisted')
        self.j.save('human-' + action + '-receipt.json', receipt)

    def run(self):
        self.provenance = verify_provenance(self.c, self.rest, self.j)
        baseline_proofs = self.proofs()
        for card in self.card_ids:
            native = self.rest.request('fixture-card', f'/api/v1/cards/{card}')
            require(native['id'] == card and native['board_id'] == BOARD and native['spec_id'] == SPEC, 'Foreign fixture Card')
        human_card = self.rest.request('human-card', f'/api/v1/cards/{self.c["human"]["card_id"]}')
        require(human_card['status'] == 'in_progress', 'Parent must authorize/prepare the normal task lifecycle before this runner')
        before = self.history()
        self.j.save('historical-record-hashes-before.json', [{'card': c, 'edition': e, 'record_id': i, 'sha256': h}
            for (c, e, i), h in before.items()])
        self.browser.board()
        self.checkpoint('initial', False, False)
        self.human('progress')
        generation = self.issue_publish('a1', 'A')
        self.associate('a1')
        self.checkpoint('a_only', True, False)
        self.issue_publish('b1', 'B', generation)
        self.associate('b1')
        self.checkpoint('both_initial', True, True)
        self.issue_publish('b_failed', 'B', generation, failed=True)
        self.checkpoint('b_failed', True, False)
        self.issue_publish('b2', 'B', generation)
        self.checkpoint('b_fresh_unassociated', True, False)
        self.associate('b2')
        self.checkpoint('both_recovered', True, True)
        self.rest.request('reset-all', self.scenario_path + '/status', 'PATCH', {'status': 'ready'})
        self.checkpoint('reset_all', False, False)
        self.issue_publish('a2', 'A', generation + 1)
        self.checkpoint('a_fresh_unassociated', False, False)
        self.associate('a2')
        self.checkpoint('a_restored_only', True, False)
        self.issue_publish('b3', 'B', generation + 1)
        self.checkpoint('b_after_reset_unassociated', True, False)
        self.associate('b3')
        self.checkpoint('both_after_reset', True, True)
        require(self.proofs() == baseline_proofs, 'Targets, accepted executions or implementation declarations changed')
        self.human('delivery_report')
        after = self.history()
        require(all(after.get(key) == value for key, value in before.items()), 'Historical record changed or disappeared')
        final_provenance = verify_provenance(self.c, self.rest, self.j)
        require(final_provenance['runtime'] == self.provenance['runtime'], 'Runtime changed during acceptance')
        result = {'state': 'completed', 'completed_at': now(), 'issued': self.issued,
            'associations': self.associations, 'history_preserved': True,
            'history_scope': 'All prior public ledger record details in editions 1..3; native omitted internal fields are not directly inspected.',
            'config_sha256': digest(canonical(self.c)), 'installed': final_provenance,
            'native_acceptance': 'The enclosing native runner, not this JSON, issues final R6 evidence.'}
        self.j.save('installed-acceptance.json', result)
        return result


def run_installed_acceptance():
    config, path, raw = load_configuration()
    journal = Journal(config, path, raw)
    journey = InstalledJourney(config, journal)
    try:
        return journey.run()
    except Exception as exc:
        journal.save('stopped.json', {'state': 'stopped', 'at': now(), 'exception_type': type(exc).__name__,
            'message': str(exc), 'automatic_retry': False,
            'instruction': 'Inspect this journal and native state before any separately authorized new run.'})
        raise
