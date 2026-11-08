import pytest
from agent_service import windows_ocr as ocr


def row(score=.99, char_scores=(.99,.99)):
    return [[], '视图', score,
            [[[10,10],[22,10],[22,25],[10,25]],[[23,10],[36,10],[36,25],[23,25]]],
            ['视','图'],list(char_scores)]


def test_character_boxes_are_from_model_alignment_not_uniform_substrings():
    assert ocr.rapid_words([row()])==[
        {'text':'视','box':[10,10,12,15]}, {'text':'图','box':[23,10,13,15]}]


@pytest.mark.parametrize('score',[.94,float('nan')])
def test_uncertain_line_cannot_authorize_click(score):
    assert ocr.rapid_words([row(score)])==[]


def test_uncertain_character_is_omitted_not_guessed():
    assert ocr.rapid_words([row(char_scores=(.9,.99))])==[]


def test_mismatched_word_boxes_fail_closed():
    item=row();item[4].append('视')
    with pytest.raises(RuntimeError,match='response_invalid'):
        ocr.rapid_words([item])


def test_response_limit_is_preserved():
    with pytest.raises(RuntimeError,match='text_limit'):
        ocr.rapid_words([row()]*151)


def test_form_scaling_retains_exact_model_coordinates_and_confidence_gate():
    normal=ocr.rapid_words([row()]);scaled=ocr.rapid_words([row()],scale=2)
    assert scaled==[{'text':w['text'],'box':[v/2 for v in w['box']]} for w in normal]
    assert ocr.rapid_words([row(char_scores=(.94,.99))],scale=2)==[]


@pytest.mark.parametrize('scale',[0,True,1.5,3])
def test_unapproved_scaling_is_denied(scale):
    with pytest.raises(RuntimeError,match='scale_invalid'):ocr.rapid_words([row()],scale=scale)
