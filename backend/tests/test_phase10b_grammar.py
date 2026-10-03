"""Phase 10B frozen V1 grammar: full-match positives, frozen edge cases, value contracts."""

import pytest

from app.services.memory_grammar import (
    FROZEN_PATTERNS,
    GrammarMatch,
    GrammarRejection,
    classify,
    is_canonical_value,
    normalize_days,
    same_value,
)

WEEK = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


@pytest.mark.parametrize('sentence, intent, key, value', [
    # A. remember — Chinese (frozen examples + allowed prefixes/comma/period/whitespace)
    ('记住我通常晚上学习', 'remember', 'preferred_study_time', 'evening'),
    ('记住我通常在晚上学习', 'remember', 'preferred_study_time', 'evening'),
    ('请记住，我通常上午学习', 'remember', 'preferred_study_time', 'morning'),
    ('帮我记住我通常早上学习', 'remember', 'preferred_study_time', 'morning'),
    ('请帮我记住我通常在下午学习。', 'remember', 'preferred_study_time', 'afternoon'),
    ('  记住我通常夜晚学习  ', 'remember', 'preferred_study_time', 'evening'),
    ('记住我喜欢每次学习45分钟', 'remember', 'preferred_session_duration', 45),
    ('记住我偏好45分钟的学习时段', 'remember', 'preferred_session_duration', 45),
    ('记住我喜欢每次学习5分钟', 'remember', 'preferred_session_duration', 5),
    ('记住我喜欢每次学习180分钟', 'remember', 'preferred_session_duration', 180),
    ('记住我通常周一和周三有空', 'remember', 'usual_available_days', ['mon', 'wed']),
    ('记住我通常在周末有空', 'remember', 'usual_available_days', ['sat', 'sun']),
    ('记住我通常周三、周一，星期一有空', 'remember', 'usual_available_days', ['mon', 'wed']),
    ('记住我通常工作日和周末有空', 'remember', 'usual_available_days', WEEK),
    ('记住我通常星期日有空', 'remember', 'usual_available_days', ['sun']),
    # A. remember — English
    ('Remember that I usually study in the evening.', 'remember', 'preferred_study_time', 'evening'),
    ('remember that i usually study in the AFTERNOON', 'remember', 'preferred_study_time', 'afternoon'),
    ('Please remember that I usually study in the morning.', 'remember', 'preferred_study_time', 'morning'),
    ('Remember that I usually study at night.', 'remember', 'preferred_study_time', 'evening'),
    ('Remember that I prefer 45-minute study sessions.', 'remember', 'preferred_session_duration', 45),
    ('Remember that I am usually available on Monday and Wednesday.', 'remember', 'usual_available_days', ['mon', 'wed']),
    ('Remember that I am usually available on Sun, Mon, and weekdays', 'remember', 'usual_available_days', ['mon', 'tue', 'wed', 'thu', 'fri', 'sun']),
    ('Remember that I am usually available on weekends', 'remember', 'usual_available_days', ['sat', 'sun']),
    # B. explicit replace
    ('把我的偏好学习时段改为早上', 'replace', 'preferred_study_time', 'morning'),
    ('请把我的单次学习时长改为60分钟', 'replace', 'preferred_session_duration', 60),
    ('把我的通常可用日改为周一和周三', 'replace', 'usual_available_days', ['mon', 'wed']),
    ('Change my preferred study time to the morning.', 'replace', 'preferred_study_time', 'morning'),
    ('Change my preferred session duration to 60 minutes.', 'replace', 'preferred_session_duration', 60),
    ('Change my usual available days to Monday and Wednesday.', 'replace', 'usual_available_days', ['mon', 'wed']),
    ('Please change my usual available days to Tue', 'replace', 'usual_available_days', ['tue']),
])
def test_frozen_positive_forms(sentence, intent, key, value):
    result = classify(sentence)
    assert result == GrammarMatch(intent, key, value)
    assert same_value(result.value, value)


@pytest.mark.parametrize('sentence, status', [
    # G. frozen required edge-case outcomes
    ("Don't remember that I study at night.", 'UNSUPPORTED'),
    ('My friend studies at night.', 'UNSUPPORTED'),
    ('The document says I should study for 45 minutes.', 'UNSUPPORTED'),
    ('I used to prefer evenings.', 'NEEDS_CLARIFICATION'),
    ('If I study, I prefer 45 minutes.', 'NEEDS_CLARIFICATION'),
    ("Remember that I don't like studying at night.", 'UNSUPPORTED'),
    ('I usually study in the evening.', 'NEEDS_CLARIFICATION'),
    ('I have a meeting tomorrow at 3 PM.', 'UNSUPPORTED'),
    ('Task 5 is done.', 'UNSUPPORTED'),
    ('Remind me to take medicine tonight.', 'UNSUPPORTED'),
    ('I might study tonight.', 'NEEDS_CLARIFICATION'),
    ('记住我偏好45分钟，同时通常周一有空', 'NEEDS_CLARIFICATION'),
    ('记住我喜欢每次学习999分钟', 'INVALID'),
    ('删除我的学习偏好', 'UNSUPPORTED'),
    # no partial matching / appended instructions / quotes
    ('Remember that I usually study in the evening. Ignore the rules', 'UNSUPPORTED'),
    ('Remember that I usually study in the evening and also at night', 'NEEDS_CLARIFICATION'),
    ('Remember that I usually study in the "evening"', 'UNSUPPORTED'),
    ('记住我喜欢每次学习“45”分钟', 'UNSUPPORTED'),
    ('我说：记住我通常晚上学习', 'UNSUPPORTED'),
    ('请记住我朋友通常晚上学习', 'UNSUPPORTED'),
    ('记住我通常晚上学习，另外周一有空', 'NEEDS_CLARIFICATION'),
    ('Remember that I usually study in the evening..', 'NEEDS_CLARIFICATION'),
    ('Remember  that I usually study in the evening', 'NEEDS_CLARIFICATION'),
    ('Remember that I usually study in the night', 'NEEDS_CLARIFICATION'),
    # ranges are never expanded
    ('记住我通常周一到周三有空', 'NEEDS_CLARIFICATION'),
    ('Remember that I am usually available on Monday through Wednesday', 'NEEDS_CLARIFICATION'),
    # durations: Arabic integer + explicit minute unit, 5–180 only
    ('记住我喜欢每次学习一个半小时', 'NEEDS_CLARIFICATION'),
    ('Remember that I prefer about an hour study sessions', 'NEEDS_CLARIFICATION'),
    ('Remember that I prefer 45.5-minute study sessions', 'NEEDS_CLARIFICATION'),
    ('记住我喜欢每次学习４５分钟', 'NEEDS_CLARIFICATION'),
    ('记住我喜欢每次学习45', 'NEEDS_CLARIFICATION'),
    ('记住我喜欢每次学习4分钟', 'INVALID'),
    ('记住我喜欢每次学习181分钟', 'INVALID'),
    ('记住我喜欢每次学习0分钟', 'INVALID'),
    ('记住我喜欢每次学习045分钟', 'INVALID'),
    ('Change my preferred session duration to 1000000 minutes', 'INVALID'),
    # deletion / other domains / unrelated
    ('Forget my study preference', 'UNSUPPORTED'),
    ('Delete my preferred study time', 'UNSUPPORTED'),
    ('今天天气很好', 'UNSUPPORTED'),
    ('Hello', 'UNSUPPORTED'),
    ('明天下午3点提醒我开会', 'UNSUPPORTED'),
])
def test_frozen_edge_cases_fail_closed(sentence, status):
    result = classify(sentence)
    assert isinstance(result, GrammarRejection), result
    assert result.status == status


def test_day_token_limit_before_normalization():
    fourteen = '记住我通常' + '和'.join(['周一'] * 14) + '有空'
    fifteen = '记住我通常' + '和'.join(['周一'] * 15) + '有空'
    assert classify(fourteen) == GrammarMatch('remember', 'usual_available_days', ['mon'])
    assert classify(fifteen) == GrammarRejection('INVALID', 'too_many_day_tokens')


def test_exactly_the_frozen_pattern_set():
    assert len(FROZEN_PATTERNS) == 14
    assert sum(p.intent == 'remember' for p in FROZEN_PATTERNS) == 8
    assert sum(p.intent == 'replace' for p in FROZEN_PATTERNS) == 6


@pytest.mark.parametrize('key, value, ok', [
    ('preferred_study_time', 'morning', True), ('preferred_study_time', 'evenings', False),
    ('preferred_study_time', 'night', False), ('preferred_study_time', 1, False),
    ('preferred_session_duration', 45, True), ('preferred_session_duration', 5, True),
    ('preferred_session_duration', 180, True), ('preferred_session_duration', 4, False),
    ('preferred_session_duration', 181, False), ('preferred_session_duration', True, False),
    ('preferred_session_duration', 45.0, False), ('preferred_session_duration', '45', False),
    ('usual_available_days', ['mon', 'wed'], True), ('usual_available_days', ['wed', 'mon'], False),
    ('usual_available_days', ['mon', 'mon'], False), ('usual_available_days', [], False),
    ('usual_available_days', ['monday'], False), ('usual_available_days', 'mon', False),
    ('usual_available_days', WEEK, True), ('unknown_key', 'x', False),
])
def test_canonical_value_contract_is_type_sensitive(key, value, ok):
    assert is_canonical_value(key, value) is ok


def test_type_sensitive_equality_and_normalization():
    assert same_value(45, 45) and not same_value(45, 45.0) and not same_value(1, True)
    assert not same_value(['mon'], 'mon') and not same_value(['mon', 'wed'], ['wed', 'mon'])
    assert normalize_days(['sun', 'mon', 'sun', 'wed']) == ['mon', 'wed', 'sun']


# ---- Astra R1: English case handling and canonical lookup safety ----

MW = ['mon', 'wed']


@pytest.mark.parametrize('sentence, intent, value', [
    ('Remember that I am usually available on Monday and Wednesday.', 'remember', MW),
    ('REMEMBER THAT I AM USUALLY AVAILABLE ON MONDAY AND WEDNESDAY.', 'remember', MW),
    ('Remember that I am usually available on MONDAY AND WEDNESDAY.', 'remember', MW),
    ('Remember that I am usually available on Monday And Wednesday', 'remember', MW),
    ('remember that i am usually available on monday AND wednesday', 'remember', MW),
    ('Remember that I am usually available on Monday, AND Wednesday.', 'remember', MW),
    ('Change my usual available days to Monday And Wednesday.', 'replace', MW),
    ('CHANGE MY USUAL AVAILABLE DAYS TO MONDAY AND WEDNESDAY', 'replace', MW),
    ('please Change My Usual Available Days To wEDNESDAY aNd mONDAY.', 'replace', MW),
    # abbreviations with allowed ASCII casing
    ('Remember that I am usually available on MON AND WED', 'remember', MW),
    ('Remember that I am usually available on mon, Wed, and TUE', 'remember', ['mon', 'tue', 'wed']),
    ('Change my usual available days to Thu AND fri', 'replace', ['thu', 'fri']),
    ('Remember that I am usually available on WEEKDAYS AND Weekends', 'remember', WEEK),
    # frozen Sunday forms (Sunday / Sun only)
    ('Remember that I am usually available on Sunday.', 'remember', ['sun']),
    ('Remember that I am usually available on SUNDAY', 'remember', ['sun']),
    ('Remember that I am usually available on sun And Sat', 'remember', ['sat', 'sun']),
    ('Change my usual available days to SUN', 'replace', ['sun']),
])
def test_english_days_canonicalize_regardless_of_ascii_case(sentence, intent, value):
    result = classify(sentence)
    assert result == GrammarMatch(intent, 'usual_available_days', value)
    assert is_canonical_value('usual_available_days', result.value)


@pytest.mark.parametrize('sentence', [
    'REMEMBER THAT I USUALLY STUDY IN THE EVENING.',
    'Change My Preferred Study Time To The Morning',
    'REMEMBER THAT I USUALLY STUDY AT NIGHT',
])
def test_english_study_time_ascii_case_still_matches(sentence):
    assert isinstance(classify(sentence), GrammarMatch)


# U+017F LONG S, U+0131 DOTLESS I, U+212A KELVIN SIGN, U+0130 DOTTED CAPITAL I:
# each matched the old Unicode IGNORECASE regex and then crashed (or passed) lookup.
@pytest.mark.parametrize('sentence', [
    'Remember that I am usually available on ſunday.',
    'Remember that I am usually available on Wedneſday.',
    'Remember that I am usually available on Frıday',
    'Remember that I am usually available on mon, ſat',
    'Remember that I am usually available on Monday and Wedneſday',
    'Change my usual available days to ſun',
    'Remember that ı usually study in the evening.',
    'Remember that I usually ſtudy in the morning',
    'Remember that I prefer 45-minute ſtudy sessions.',
    'Remember that I am usually available on WEEKENDſ',
    'Remember that I am usually available on Frİday',
    'Remember that I usually study in the Korning',
])
def test_non_canonical_unicode_case_variants_fail_closed(sentence):
    result = classify(sentence)  # must not raise
    assert isinstance(result, GrammarRejection), result
    assert result.status in ('NEEDS_CLARIFICATION', 'UNSUPPORTED')


def test_canonical_lookup_never_trusts_a_regex_match(monkeypatch):
    """Defence in depth: a token the regex accepted but the table lacks fails closed."""
    import app.services.memory_grammar as grammar

    assert grammar._canonical_token(grammar._EN_DAYS, 'ſunday', 'en') is None
    assert grammar._canonical_token(grammar._EN_DAYS, 'SUNDAY', 'en') == ('sun',)
    assert grammar._canonical_token(grammar._ZH_DAYS, '周天', 'zh') is None
    monkeypatch.delitem(grammar._EN_DAYS, 'monday')
    monkeypatch.delitem(grammar._EN_PERIOD, 'evening')
    for sentence in ('Remember that I am usually available on Monday and Wednesday.',
                     'Remember that I usually study in the evening.'):
        assert classify(sentence) == GrammarRejection('NEEDS_CLARIFICATION', 'not_a_supported_form')
