"""Owned read worker for explicitly versioned public-project comparisons."""
from dataclasses import asdict
import json
import socket
import ssl
import sys

from .browser import SourceError
from .contracts import BrowserReadError,Observation,PolicyError,utc_now
from .project_documents import ResourceCapture,ResourceReplay,document,extract
from .project_scope import project_scope


def capture(url,query,fetch,at):
    resources=ResourceCapture(fetch)
    mime,content=document(url,resources)
    obs=extract(url,mime,content,query,at)
    proof={'version':1,'url':url,'query':query,'captured_at':at,'resources':resources.resources}
    return {'observation':asdict(obs),'document_proof':proof}


def validate_read(value,url,query):
    try:
        if type(value) is not dict or set(value)!={'observation','document_proof'}:
            raise ValueError()
        proof=value['document_proof']
        if (type(proof) is not dict or set(proof)!={'version','url','query','captured_at','resources'}
                or type(proof['version']) is not int or proof['version']!=1
                or proof['url']!=url or proof['query']!=query):
            raise ValueError()
        replay=ResourceReplay(proof['resources'])
        expected=capture(url,query,replay,proof['captured_at'])
        if replay.cursor!=len(replay.rows) or json.dumps(value,sort_keys=True)!=json.dumps(expected,sort_keys=True):
            raise ValueError()
        return Observation(**expected['observation'])
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
        raise BrowserReadError('source_rejected') from None


class ProjectReaderProcess:
    def __init__(self,query=None,*,include_attribution=True):
        self.query=query
        self.include_attribution=include_attribution
        self.last_document_proof=None

    def read(self,url,should_stop=lambda:False):
        self.last_document_proof=None
        if project_scope(url) is None:
            from .browser import BrowserProcess
            return BrowserProcess(self.query,include_attribution=self.include_attribution).read(url,should_stop)
        from .process import run_child
        try:
            raw=run_child([sys.executable,'-B','-m','agent_service.project_reader',
                json.dumps({'url':url,'query':self.query})],should_stop=should_stop,timeout=40)
            if len(raw.encode())>2_000_000:raise SourceError('Result too large')
            value=json.loads(raw)
            if 'error_code' in value:raise BrowserReadError(value['error_code'])
            obs=validate_read(value,url,self.query)
            self.last_document_proof=value['document_proof']
            return obs
        except TimeoutError:raise BrowserReadError('network_timeout') from None
        except (OSError,RuntimeError) as exc:
            if isinstance(exc,BrowserReadError):raise
            raise BrowserReadError('network_failed') from None


def main():
    if sys.stdin.buffer.read(1)!=b'1':return 2
    try:
        request=json.loads(sys.argv[1])
        if set(request)!={'url','query'} or len(sys.argv)!=2:raise ValueError()
        if request['query'] is not None and (type(request['query']) is not str or len(request['query'])>500):
            raise ValueError()
        from .project_http import fetch
        value=capture(request['url'],request['query'],fetch,utc_now())
    except Exception as exc:
        code=('dns_failed' if isinstance(exc,socket.gaierror) else
              'tls_failed' if isinstance(exc,ssl.SSLError) else
              'network_timeout' if isinstance(exc,TimeoutError) else
              'policy_rejected' if isinstance(exc,PolicyError) else
              'source_rejected' if isinstance(exc,(SourceError,ValueError)) else 'network_failed')
        value={'error_code':code}
    sys.stdout.buffer.write(json.dumps(value,ensure_ascii=False).encode())
    return 0

if __name__=='__main__':raise SystemExit(main())
