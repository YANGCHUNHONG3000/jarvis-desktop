"""Proof v5 reconstructs dynamic project documents from retained HTTP resources."""
import json

from .project_documents import PURPOSE, ResourceCapture, ResourceReplay, document
from .computer import ComputerError


def discover(request,fetch,searched_at,*,compact=True):
    from .comparison_crosssite_discovery import _discover_attribution, MAX_RESULT_CHARS
    resources=ResourceCapture(fetch)
    value=_discover_attribution(request,lambda url:document(url,resources),searched_at)
    if compact:
        # Every rendered byte is reproducible from resources; do not store it twice.
        for page in value['proof']['pages']:
            page.pop('html_zlib_base64',None)
        value['proof']['representation']='resource-replay-v1'
    value['proof']['resources']=resources.resources
    if len(json.dumps(value,ensure_ascii=False).encode())>MAX_RESULT_CHARS:
        raise ComputerError('supplement_crosssite_unavailable')
    return value


def validate(value,request):
    from .comparison_crosssite_discovery import MAX_RESULT_CHARS
    try:
        if (type(value) is not dict or set(value)!={'discovery','proof'}
                or len(json.dumps(value,ensure_ascii=False).encode())>MAX_RESULT_CHARS
                or type(value['proof']) is not dict):
            raise ValueError()
        proof=value['proof']
        compact='representation' in proof
        expected_fields={'version',*request,'pages','chosen','chosen_urls','searched_at','resources'}
        if compact:expected_fields.add('representation')
        if (set(proof)!=expected_fields
                or compact and proof['representation']!='resource-replay-v1'
                or type(proof['version']) is not int or proof['version']!=5
                or any(proof.get(k)!=v for k,v in request.items())):
            raise ValueError()
        replay=ResourceReplay(proof['resources'])
        expected=discover(request,replay,proof['searched_at'],compact=compact)
        if replay.cursor!=len(replay.rows) or json.dumps(value,sort_keys=True)!=json.dumps(expected,sort_keys=True):
            raise ValueError()
        return expected
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,ComputerError):
        raise ComputerError('supplement_crosssite_proof_invalid') from None
