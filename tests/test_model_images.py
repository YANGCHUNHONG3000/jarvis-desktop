"""Offline PNG request, accounting and compatibility contracts; no real API/key."""
import base64
from io import BytesIO
import json
import os
import struct
import zlib

from PIL import Image
import pytest
import requests

from agent_service import model_images, openai_profile
from agent_service.budget import BudgetError
from agent_service.model import ModelError, request_token_bound
from test_openai_model import FakeSession, setup_client, success


def png(size=(24, 16), *, format='PNG', noise=False):
    image = (Image.frombytes('RGB', size, os.urandom(size[0] * size[1] * 3)) if noise
             else Image.new('RGB', size, 'navy'))
    with BytesIO() as output:
        image.save(output, format=format)
        return output.getvalue()


def png_chunk(kind, data):
    return (struct.pack('>I', len(data)) + kind + data
            + struct.pack('>I', zlib.crc32(kind + data)))


def text_bound_from_post(session):
    body = json.loads(session.posts[0][1]['data'])
    content = body['messages'][1]['content']
    if isinstance(content, list):
        body['messages'][1]['content'] = content[0]['text']
    return request_token_bound(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode())


@pytest.mark.parametrize('count', [1, 2])
def test_png_request_reserved_and_settled_using_separate_image_ceiling(setup_client, count):
    make, ledger, _ = setup_client
    images = [png((32 + index, 16)) for index in range(count)]
    session = FakeSession(result=success(usage={'prompt_tokens': 1600 * count,
                                               'completion_tokens': 4,
                                               'total_tokens': 1600 * count + 4}))
    client = make(session)
    assert client.complete('JSON only', 'Describe these.', max_tokens=32,
                           images_png=images) == {'answer': 'ok'}
    body = json.loads(session.posts[0][1]['data'])
    assert body['messages'][1]['content'] == [
        {'type': 'text', 'text': 'Describe these.'}, *[
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + base64.b64encode(raw).decode(),
                'detail': 'high'}} for raw in images]]
    bound = text_bound_from_post(session) + count * 65_536
    assert client.last_call['prompt_token_bound'] == bound
    assert client.last_call['reserved_micro'] == bound * openai_profile.INPUT_RATE + 32 * openai_profile.OUTPUT_RATE
    assert client.last_call['budget_policy'] == 'request_png_bound_v1'
    assert client.last_call['image_count'] == count
    assert client.calls == 1 and len(session.posts) == 1 and len(session.gets) == 3
    assert ledger.snapshot()['held_micro'] == 0
    assert ledger.snapshot()['charged_micro'] == 1600 * count * openai_profile.INPUT_RATE + 4 * openai_profile.OUTPUT_RATE
    assert 'data:image/' not in repr(client.last_call)
    with ledger._transaction(write=False) as db:
        rows = db.execute('SELECT request_hash, usage_json FROM reservations').fetchall()
    assert len(rows) == 1 and len(rows[0][0]) == 64
    assert 'data:image/' not in repr(rows)


@pytest.mark.parametrize('images', [None, [], ()])
def test_empty_images_keep_exact_text_request_and_original_budget_policy(setup_client, images):
    make, _, _ = setup_client
    plain, optional = FakeSession(), FakeSession()
    make(plain).complete('system', 'user', max_tokens=32)
    client = make(optional)
    client.complete('system', 'user', max_tokens=32, images_png=images)
    assert plain.posts[0][1]['data'] == optional.posts[0][1]['data']
    assert client.last_call['prompt_token_bound'] == request_token_bound(optional.posts[0][1]['data'])
    assert client.last_call['budget_policy'] == 'request_utf8_v1'
    assert client.last_call['image_count'] == 0


@pytest.mark.parametrize('bad', [True, False, 'screenshot.png', b'raw', {}, 1,
                                [True], [bytearray(b'PNG')], [memoryview(b'PNG')],
                                [None], [b''], [b'not a PNG'], [png(format='JPEG')],
                                [png()] * 3, [png() + b'trailing']])
def test_bad_image_inputs_fail_before_key_network_or_reservation(setup_client, bad):
    make, ledger, _ = setup_client
    session = FakeSession()
    client = make(session)
    client.key_path.unlink()  # A missing key must not hide local image rejection.
    with pytest.raises(ModelError, match='^model_image_invalid$'):
        client.complete('JSON', 'user', images_png=bad)
    assert not session.gets and not session.posts and client.calls == 0
    assert ledger.snapshot()['held_micro'] == 0 and client.last_call is None


@pytest.mark.parametrize('size', [(2049, 1), (1, 2049)])
def test_png_dimension_limit_is_local(setup_client, size):
    make, ledger, _ = setup_client
    session = FakeSession()
    with pytest.raises(ModelError, match='^model_image_invalid$'):
        make(session).complete('JSON', 'user', images_png=[png(size)])
    assert not session.gets and ledger.snapshot()['held_micro'] == 0


def test_boundary_dimensions_allowed():
    raw = png((2048, 2048))
    assert model_images.validate_png_images([raw]) == (raw,)


@pytest.mark.parametrize('damage', ['crc', 'truncated', 'zero-width', 'apng', 'fake-signature'])
def test_png_container_and_pixels_are_checked(setup_client, damage):
    raw = png()
    if damage == 'crc':
        raw = raw[:29] + bytes([raw[29] ^ 1]) + raw[30:]
    elif damage == 'truncated':
        raw = raw[:40] + raw[-12:]
    elif damage == 'zero-width':
        header = b'\0\0\0\0' + raw[20:29]
        raw = raw[:8] + png_chunk(b'IHDR', header) + raw[33:]
    elif damage == 'apng':
        raw = raw[:33] + png_chunk(b'acTL', struct.pack('>II', 1, 0)) + raw[33:]
    else:
        raw = model_images.PNG_SIGNATURE + png(format='JPEG') + model_images.PNG_END
    make, ledger, _ = setup_client
    session = FakeSession()
    with pytest.raises(ModelError, match='^model_image_invalid$'):
        make(session).complete('JSON', 'user', images_png=[raw])
    assert not session.gets and ledger.snapshot()['held_micro'] == 0


def test_actual_animated_png_is_rejected():
    with BytesIO() as output:
        Image.new('RGB', (16, 16), 'red').save(
            output, format='PNG', save_all=True,
            append_images=[Image.new('RGB', (16, 16), 'blue')], duration=100)
        with pytest.raises(ValueError, match='^model_image_invalid$'):
            model_images.validate_png_images([output.getvalue()])


def test_png_bytes_limit_applies_to_total_before_decoding(setup_client):
    make, ledger, _ = setup_client
    raw = b'x' * (model_images.MAX_TOTAL_PNG_BYTES // 2 + 1)
    session = FakeSession()
    with pytest.raises(ModelError, match='^model_image_bytes_limit$'):
        make(session).complete('JSON', 'user', images_png=[raw, raw])
    assert not session.gets and not session.posts and ledger.snapshot()['held_micro'] == 0


def test_base64_transport_size_is_not_a_text_token_count(setup_client):
    make, _, _ = setup_client
    session = FakeSession()
    client = make(session)
    client.complete('JSON', 'Describe', images_png=[png((512, 512), noise=True)], max_tokens=32)
    assert len(session.posts[0][1]['data']) > openai_profile.SHORT_INPUT_LIMIT
    assert client.last_call['prompt_token_bound'] == text_bound_from_post(session) + 65_536
    assert client.last_call['prompt_token_bound'] < openai_profile.SHORT_INPUT_LIMIT


def test_image_ceiling_included_in_short_input_limit_before_key_io(setup_client, monkeypatch):
    make, ledger, _ = setup_client
    session = FakeSession()
    client = make(session)
    client.key_path.unlink()
    monkeypatch.setattr(openai_profile, 'SHORT_INPUT_LIMIT', 65_536)
    with pytest.raises(ModelError, match='^model_input_exceeds_context$'):
        client.complete('JSON', 'Describe', images_png=[png()])
    assert not session.gets and ledger.snapshot()['held_micro'] == 0


def test_deepseek_images_rejected_before_key_io_or_network(setup_client):
    make, ledger, permission = setup_client
    permit = json.loads(permission.read_text())
    permit['provider'] = 'deepseek'
    permission.write_text(json.dumps(permit))
    session = FakeSession()
    client = make(session)
    client.key_path.unlink()
    with pytest.raises(ModelError, match='^model_images_provider_unsupported$'):
        client.complete('JSON', 'Describe', images_png=[png()])
    assert not session.gets and not session.posts and ledger.snapshot()['held_micro'] == 0


@pytest.mark.parametrize('failure', ['prices', 'fx', 'model'])
def test_images_keep_price_fx_and_model_preflights(setup_client, failure):
    make, ledger, _ = setup_client
    options = {'prices': b'bad'} if failure == 'prices' else (
        {'fx': b'bad'} if failure == 'fx' else {'metadata': {'data': []}})
    session = FakeSession(**options)
    with pytest.raises(ModelError):
        make(session).complete('JSON', 'Describe', images_png=[png()])
    assert not session.posts and ledger.snapshot()['held_micro'] == 0


@pytest.mark.parametrize('failure', ['unknown-usage', 'timeout', 'identity'])
def test_uncertain_image_charge_keeps_hold_and_does_not_retry(setup_client, failure):
    make, ledger, _ = setup_client
    session = FakeSession(result=success(usage={}) if failure == 'unknown-usage' else
                          success(model='unexpected-model') if failure == 'identity' else success())
    if failure == 'timeout':
        original = session.post
        def timeout(*args, **kwargs):
            original(*args, **kwargs)
            raise requests.Timeout('offline-only')
        session.post = timeout
    client = make(session)
    with pytest.raises(ModelError):
        client.complete('JSON', 'Describe', images_png=[png()])
    assert len(session.posts) == 1 and client.calls == 1
    assert client.last_call['state'] == 'pending'
    assert ledger.snapshot()['held_micro'] == client.last_call['reserved_micro']
    assert ledger.snapshot()['charged_micro'] == 0


def test_image_usage_exceeding_application_bound_settles_fault_and_stops_calls(setup_client):
    make, ledger, _ = setup_client
    usage = {'prompt_tokens': 100_000, 'completion_tokens': 4, 'total_tokens': 100_004}
    session = FakeSession(result=success(usage=usage))
    client = make(session)
    with pytest.raises(ModelError, match='^model_usage_exceeds_request_bound$'):
        client.complete('JSON', 'Describe', images_png=[png()])
    with ledger._transaction(write=False) as db:
        assert db.execute('SELECT COUNT(*) FROM budget_faults').fetchone()[0] == 1
    assert ledger.snapshot()['held_micro'] == 0
    assert ledger.snapshot()['charged_micro'] == 100_000 * openai_profile.INPUT_RATE + 4 * openai_profile.OUTPUT_RATE
    with pytest.raises(BudgetError, match='accounting fault'):
        client.complete('JSON', 'Describe', images_png=[png()])
    assert len(session.posts) == 1


@pytest.mark.parametrize('stage', ['initial', 'preflight', 'reserved'])
def test_cancelled_image_request_preserves_existing_hold_semantics(setup_client, monkeypatch, stage):
    make, ledger, _ = setup_client
    stopped = [stage == 'initial']
    session = FakeSession(on_get=lambda url: stopped.__setitem__(0, True)
                          if stage == 'preflight' and url == openai_profile.FX else None)
    if stage == 'reserved':
        reserve = ledger.reserve
        def reserve_then_stop(*args):
            reservation = reserve(*args)
            stopped[0] = True
            return reservation
        monkeypatch.setattr(ledger, 'reserve', reserve_then_stop)
    client = make(session, should_stop=lambda: stopped[0])
    with pytest.raises(ModelError, match='^model_cancelled_before_request$'):
        client.complete('JSON', 'Describe', images_png=[png()])
    assert not session.posts
    assert ledger.snapshot()['held_micro'] == (client.last_call['reserved_micro'] if stage == 'reserved' else 0)


def test_returned_image_usage_settles_even_if_cancel_arrives_after_post(setup_client):
    make, ledger, _ = setup_client
    stopped = [False]
    session = FakeSession()
    post = session.post
    def post_then_stop(*args, **kwargs):
        response = post(*args, **kwargs)
        stopped[0] = True
        return response
    session.post = post_then_stop
    client = make(session, should_stop=lambda: stopped[0])
    assert client.complete('JSON', 'Describe', images_png=[png()]) == {'answer': 'ok'}
    assert client.last_call['state'] == 'settled' and ledger.snapshot()['held_micro'] == 0


def test_image_reservation_failure_prevents_paid_post(setup_client, monkeypatch):
    make, ledger, _ = setup_client
    session = FakeSession()
    def unavailable(*args):
        raise BudgetError('offline ledger unavailable')
    monkeypatch.setattr(ledger, 'reserve', unavailable)
    client = make(session)
    with pytest.raises(BudgetError, match='offline ledger unavailable'):
        client.complete('JSON', 'Describe', images_png=[png()])
    assert not session.posts and client.calls == 0 and client.last_call is None
