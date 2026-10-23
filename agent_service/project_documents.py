"""Versioned read-only project snapshots; GitLab bootstrap data is never executed."""
from html import escape
import json
import re
from urllib.parse import urljoin, urlsplit, parse_qsl

from .browser import SourceError, _excerpt
from .project_scope import project_scope, same_project

PURPOSE = 'dynamic_project_attribution'
POLICY = 'project_attribution_v4'
MAX_RESOURCES = 8
MAX_BYTES = 2 * 1024 * 1024
PROFILE = {'direct-pinned-v1', 'hf-loopback-connect-v1'}


def enabled(goal):
    return goal.get('comparison', {}).get('publisher_policy') in {POLICY, 'project_attribution_v5'}


def _reject():
    raise SourceError('Project document schema or scope rejected')


def _unique(items):
    out = {}
    for key, value in items:
        if key in out:
            _reject()
        out[key] = value
    return out


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique)


def _soup(raw):
    from bs4 import BeautifulSoup
    return BeautifulSoup(raw, 'html.parser')


def _gitlab(url, content, fetch):
    """Reconstruct dynamic content only from exact scoped, bounded GET schemas."""
    scope = project_scope(url)
    soup = _soup(content)
    body = soup.find('body')
    project = '/'.join(scope[1:])
    if body is None or body.get('data-project-full-path') != project:
        _reject()
    prefix = '/' + project + '/-/'
    tail = urlsplit(url).path[len(prefix):].split('/')
    if tail[0] not in {'tree', 'blob'}:
        return content
    palettes = [t.get('data-command-palette') for t in soup.find_all(attrs={'data-command-palette': True})]
    if len(palettes) != 1:
        _reject()
    palette = _json(palettes[0])
    if type(palette) is not dict:
        _reject()
    base = palette.get('project_blob_url')
    if type(base) is not str or not base.startswith(prefix+'blob/'):
        _reject()
    ref = base[len(prefix+'blob/'):]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', ref) or ref in {'.', '..'}:
        _reject()
    source_prefix = prefix + tail[0] + '/' + ref
    if urlsplit(url).path != source_prefix and not urlsplit(url).path.startswith(source_prefix + '/'):
        _reject()
    # Require agreement with the page's own project/ref metadata.
    declared = soup.find_all(attrs={'data-project-path': project, 'data-ref': ref})
    if not declared:
        _reject()
    fragments = []
    title = soup.title.get_text(' ', strip=True) if soup.title else project

    def blob(endpoint, expected_path):
        mime, raw = fetch(endpoint)
        if not mime.startswith('application/json'):
            _reject()
        value = _json(raw)
        if (type(value) is not dict or value.get('path') != expected_path
                or value.get('binary') is not False or value.get('render_error')
                or type(value.get('html')) is not str or not value['html'].strip()):
            _reject()
        html = value['html']
        links = []
        for key, label in [('raw_path', 'Raw file'), ('commits_path', 'File commit history')]:
            target = urljoin(url, value.get(key, ''))
            if same_project(url, target) and target != url:
                links.append(f'<a href="{escape(target, quote=True)}">{label}</a>')
        return '<h1>' + escape(expected_path) + '</h1>' + html + ''.join(links)

    if tail[0] == 'blob':
        path = urlsplit(url).path[len(base)+1:]
        if not path or not any(t.get('data-blob-path') == path for t in declared):
            _reject()
        fragments.append(blob(url + '?format=json&viewer=simple', path))
    else:
        calls = []
        for script in soup.find_all('script', src=False):
            text = script.get_text()
            for match in re.finditer(r'\bgl\.startup_calls\s*=\s*', text):
                # Only the JSON literal is parsed, never evaluated as JS.
                value, end = json.JSONDecoder(object_pairs_hook=_unique).raw_decode(text[match.end():])
                if not text[match.end()+end:].lstrip().startswith(';'):
                    _reject()
                if value is not None:
                    calls.append(value)
        if len(calls) != 1 or type(calls[0]) is not dict or len(calls[0]) > 4:
            _reject()
        folder = urlsplit(url).path[len(source_prefix):].strip('/')
        logs_path = prefix+'refs/'+ref+'/logs_tree/' + (folder+'/' if folder else '')
        for endpoint in calls[0]:
            p = urlsplit(endpoint)
            if p.scheme or p.netloc or p.fragment or '%' in p.path or '\\' in endpoint:
                _reject()
            params = parse_qsl(p.query, keep_blank_values=True)
            if p.path == logs_path and dict(params) == {'format':'json','offset':'0','ref_type':'HEADS'} and len(params)==3:
                mime, raw = fetch(urljoin(url, endpoint))
                if not mime.startswith('application/json'):
                    _reject()
                entries = _json(raw)
                if type(entries) is not list or len(entries)>100:
                    _reject()
                links=[]
                for item in entries:
                    name = item.get('file_name') if type(item) is dict else None
                    if (type(name) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]+', name)
                            or name in {'.','..'}):
                        continue
                    target=urljoin(url,base+'/'+(folder+'/' if folder else '')+name)
                    if not same_project(url,target):
                        _reject()
                    links.append(f'<a href="{escape(target,quote=True)}">{escape(name)}</a>')
                fragments.append('<nav data-jarvis-origin="declared-file-list-and-template">'+''.join(links)+'</nav>')
            elif (p.path.startswith(base+'/') and dict(params)=={'format':'json','ref_type':'heads','viewer':'rich'}
                    and len(params)==3 and same_project(url,urljoin(url,p.path))):
                fragments.append(blob(urljoin(url,endpoint),p.path[len(base)+1:]))
            else:
                _reject()
        if not fragments:
            _reject()
    return ('<html><head><title>'+escape(title)+'</title></head><body>'+''.join(fragments)+'</body></html>').encode()


def document(url, fetch):
    scope=project_scope(url)
    if scope is None:
        _reject()
    mime, content=fetch(url)
    if scope[0]=='gitlab' and mime.startswith('text/html') and '/-/' in url:
        route=urlsplit(url).path.split('/-/')[1].split('/')[0]
        if route in {'tree','blob'}:
            return 'text/html; charset=utf-8', _gitlab(url,content,fetch)
    if not (mime.startswith('text/html') or mime.startswith('text/plain')):
        _reject()
    return mime,content


class ResourceCapture:
    """The complete ordered transport transcript; replay never touches network."""
    def __init__(self, fetch):
        self.fetch, self.resources, self.total = fetch, [], 0

    def __call__(self,url):
        from .comparison_crosssite_discovery import _pack
        if len(self.resources)>=MAX_RESOURCES or self.total>=MAX_BYTES:
            _reject()
        row={'url':url}
        self.resources.append(row)
        try:
            mime,content,transport=self.fetch(url)
            if (type(content) is not bytes or not content or len(content)>512*1024
                    or type(mime) is not str or len(mime)>300 or transport not in PROFILE
                    or transport=='hf-loopback-connect-v1' and urlsplit(url).netloc!='huggingface.co'):
                _reject()
            self.total+=len(content)
            if self.total>MAX_BYTES:
                _reject()
            from hashlib import sha256
            row.update(status='ok',mime=mime,transport=transport,
                       sha256=sha256(content).hexdigest(),body=_pack(content))
            return mime,content
        except Exception:
            row.clear();row.update(url=url,status='unavailable')
            raise


class ResourceReplay:
    def __init__(self,rows):
        if type(rows) is not list or not 1<=len(rows)<=MAX_RESOURCES:
            _reject()
        self.rows,self.cursor=rows,0

    def __call__(self,url):
        from .comparison_crosssite_discovery import _unpack
        from hashlib import sha256
        if self.cursor>=len(self.rows):
            _reject()
        row=self.rows[self.cursor];self.cursor+=1
        if type(row) is not dict or row.get('url')!=url:
            _reject()
        if row=={'url':url,'status':'unavailable'}:
            raise OSError('Retained failed project GET')
        if set(row)!={'url','status','mime','transport','sha256','body'} or row['status']!='ok':
            _reject()
        content=_unpack(row['body'])
        if sha256(content).hexdigest()!=row['sha256']:
            _reject()
        return row['mime'],content,row['transport']


def extract(url,mime,content,query,captured_at):
    from .contracts import Observation
    if mime.startswith('text/plain'):
        title='';text=content.decode('utf8',errors='replace')
    else:
        soup=_soup(content)
        title=soup.title.get_text(' ',strip=True) if soup.title else ''
        for tag in soup(['script','style','template','noscript']):tag.decompose()
        text=(soup.body or soup).get_text('\n',strip=True)
    if not text.strip():_reject()
    return Observation(url,url,title[:300],_excerpt(text.strip(),query),captured_at)
