"""Optional text-only Chat Completions adapter; never receives account credentials."""
import json
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'llm.local.json'
PROMPT_PATH = Path(__file__).resolve().parents[1] / 'config' / 'llm_prompt.txt'
CHOICE_MODES = {15, 16, 17, 18, 41, 42, 43, 44}
SPELL_MODES = {51, 52, 53, 54, 73}
SUPPORTED_MODES = CHOICE_MODES | SPELL_MODES | {32}
_CACHE = OrderedDict()


def load_config():
    config = {}
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('invalid config')
    if config.get('enabled') is not True:
        return None
    key = os.environ.get(config.get('api_key_env', 'CIDAREN_LLM_API_KEY'), '')
    base = str(config.get('base_url', '')).rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('base_url must use HTTPS without credentials/query')
    model = config.get('model')
    if not key or not isinstance(model, str) or not model.strip():
        raise ValueError('missing key or model')
    timeout = float(config.get('timeout_seconds', 15))
    if not 1 <= timeout <= 60:
        raise ValueError('invalid timeout')
    return {'base_url': base, 'model': model, 'key': key, 'timeout': timeout}


def _choices(exam):
    """Use our own indices, then map back to the original submission tags."""
    result = []
    mode = exam['topic_mode']
    for index, option in enumerate(exam.get('options') or []):
        if not isinstance(option, dict):
            continue
        tag = option.get('answer_tag')
        if mode in {15, 16, 17, 18}:
            result.append((str(index), option.get('content'), index))
        elif type(tag) is int:
            result.append((str(index), option.get('content'), tag))
        elif isinstance(tag, str):
            subs = option.get('sub_options') or []
            if subs:
                for sub_index, sub in enumerate(subs):
                    if isinstance(sub, dict) and 'answer_tag' in sub:
                        result.append((f'{index}:{sub_index}', sub.get('content'), tag + str(sub['answer_tag'])))
            else:
                result.append((str(index), option.get('content'), tag + '0'))
    return result


def build_payload(exam, word_dict):
    mode = exam.get('topic_mode')
    if mode not in SUPPORTED_MODES:
        return None
    stem = exam.get('stem') or {}
    if not isinstance(stem, dict):
        return None
    content = stem.get('content') or ''
    remark = stem.get('remark') or ''
    if not isinstance(content, str) or not isinstance(remark, str) or not (content.strip() or remark.strip()):
        return None
    choices = _choices(exam) if mode in CHOICE_MODES else []
    if mode in CHOICE_MODES and not choices:
        return None
    if mode == 73:
        prefixes = re.findall(r'\{([^{}]*)\}', content)
        count = len(prefixes)
    elif mode in SPELL_MODES:
        prefixes = [exam.get('w_tip') or '']
        count = 1
    elif mode == 32:
        count = len(re.findall(r'_', content))
        prefixes = [''] * count
    else:
        count, prefixes = 0, []
    if mode not in CHOICE_MODES and not 1 <= count <= 20:
        return None
    # Whitelist fields only: topic_code, tokens, IDs and audio URLs never leave here.
    payload = {'mode': mode, 'content': content, 'remark': remark,
               'choices': [{'id': i, 'text': text} for i, text, _ in choices],
               'blank_count': count, 'prefixes': prefixes,
               'word_lengths': exam.get('w_lens') or [],
               'word_bank': [o['content'] for o in (exam.get('options') or [])
                             if isinstance(o, dict) and isinstance(o.get('content'), str)] if mode not in CHOICE_MODES else []}
    relevant = []
    for zh, en in (word_dict or {}).items():
        if isinstance(zh, str) and isinstance(en, str) and zh and (zh in remark or zh in content):
            relevant.append({'zh': zh, 'en': en})
            if len(relevant) >= 20:
                break
    payload['course_words'] = relevant
    # Avoid silently cutting off the question/constraints.
    if len(json.dumps(payload, ensure_ascii=False)) > 16000:
        return None
    return payload


def validate_answer(exam, payload, result):
    if not isinstance(result, dict) or result.get('status') != 'ok':
        return None
    if exam['topic_mode'] in CHOICE_MODES:
        choice_id = result.get('choice_id')
        if not isinstance(choice_id, str):
            return None
        return next((tag for i, _, tag in _choices(exam) if i == choice_id), None)
    words = result.get('words')
    if not isinstance(words, list) or len(words) != payload['blank_count']:
        return None
    if any(not isinstance(w, str) or not re.fullmatch(r"[A-Za-z]+(?:[-' ][A-Za-z]+)*", w) for w in words):
        return None
    for i, word in enumerate(words):
        prefix = payload['prefixes'][i]
        if not word.lower().startswith(prefix.lower()):
            return None
        lengths = payload['word_lengths']
        if i < len(lengths) and lengths[i] and len(word) != int(lengths[i]):
            return None
    if exam['topic_mode'] in SPELL_MODES and any(' ' in w for w in words):
        return None
    bank = payload['word_bank']
    if bank and exam['topic_mode'] in {32, 73}:
        if any(w.lower() not in {b.lower() for b in bank} for w in words):
            return None
    if exam['topic_mode'] == 73:
        return json.dumps(words, ensure_ascii=False)
    if exam['topic_mode'] == 32:
        return ','.join(words)
    return words[0]


def solve(public_info):
    """One request at most; any failure returns None so the caller can skip."""
    try:
        config = load_config()
        if not config:
            return None
        exam = public_info.exam
        payload = build_payload(exam, getattr(public_info, 'word_dict', {}))
        if payload is None:
            return None
        prompt = PROMPT_PATH.read_text(encoding='utf-8')
        user_text = json.dumps(payload, ensure_ascii=False)
        cache_key = (config['base_url'], config['model'], prompt, user_text)
        if cache_key in _CACHE:
            _CACHE.move_to_end(cache_key)
            return _CACHE[cache_key]
        # Plain requests, not the platform's authenticated session. Do not follow
        # redirects carrying the API key to a different destination.
        with requests.Session() as session:
            response = session.post(
                config['base_url'] + '/chat/completions',
                headers={'Authorization': 'Bearer ' + config['key']},
                json={'model': config['model'], 'messages': [
                    {'role': 'system', 'content': prompt},
                    {'role': 'user', 'content': user_text}],
                    'max_tokens': 256, 'stream': False},
                timeout=(5, config['timeout']), allow_redirects=False)
            if response.status_code != 200:
                logger.warning('LLM HTTP failure (%s); skipping', response.status_code)
                return None
            data = response.json()
        choice = data['choices'][0]
        if choice.get('finish_reason') != 'stop':
            return None
        result = json.loads(choice['message']['content'])
        answer = validate_answer(exam, payload, result)
        if answer is not None:
            _CACHE[cache_key] = answer
            if len(_CACHE) > 256:
                _CACHE.popitem(last=False)
        return answer
    except Exception as exc:
        # Exception text/response bodies can contain secrets: log type only.
        logger.warning('LLM unavailable (%s); skipping', type(exc).__name__)
        return None
