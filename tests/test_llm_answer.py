import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api import llm_answer as llm


class LLMTests(unittest.TestCase):
    def setUp(self):
        llm._CACHE.clear()
        self.exam = {'topic_mode': 42, 'stem': {'content': 'It is {}.', 'remark': '重要'},
                     'options': [{'content': 'important', 'answer_tag': 7}],
                     'topic_code': 'SECRET', 'token': 'SECRET'}
        self.info = SimpleNamespace(exam=self.exam, word_dict={'重要': 'important'})
        self.config = {'base_url': 'https://provider.example/v1', 'model': 'test',
                       'key': 'API_SECRET', 'timeout': 15}

    def call_model(self, content, status=200, finish='stop'):
        session = MagicMock()
        response = session.__enter__.return_value.post.return_value
        response.status_code = status
        response.json.return_value = {'choices': [{'finish_reason': finish,
            'message': {'content': content}}]}
        return session

    def test_only_whitelisted_fields(self):
        payload = llm.build_payload(self.exam, self.info.word_dict)
        self.assertNotIn('SECRET', json.dumps(payload))
        self.assertEqual(payload['course_words'][0]['en'], 'important')

    def test_choice_mapping_and_invalid_ids(self):
        payload = llm.build_payload(self.exam, {})
        self.assertEqual(llm.validate_answer(self.exam, payload, {'status': 'ok', 'choice_id': '0'}), 7)
        for value in ['7', 'fake', 0, True]:
            self.assertIsNone(llm.validate_answer(self.exam, payload, {'status': 'ok', 'choice_id': value}))

    def test_nested_choice_mapping(self):
        self.exam['options'] = [{'answer_tag': '1#', 'sub_options': [
            {'content': 'important', 'answer_tag': 2}]}]
        payload = llm.build_payload(self.exam, {})
        self.assertEqual(llm.validate_answer(self.exam, payload, {'status': 'ok', 'choice_id': '0:0'}), '1#2')

    def test_spelling_constraints(self):
        exam = {'topic_mode': 73, 'stem': {'content': '{im} {}'}, 'w_lens': [9, 4]}
        payload = llm.build_payload(exam, {})
        good = {'status': 'ok', 'words': ['important', 'work']}
        self.assertEqual(json.loads(llm.validate_answer(exam, payload, good)), good['words'])
        for words in [['important'], ['important', 'works'], ['different', 'work'], ['im', 'work']]:
            self.assertIsNone(llm.validate_answer(exam, payload, {'status': 'ok', 'words': words}))

    def test_phrase_bank_and_count(self):
        exam = {'topic_mode': 32, 'stem': {'content': '_ and _'},
                'options': [{'content': 'all our'}, {'content': 'work'}]}
        payload = llm.build_payload(exam, {})
        self.assertEqual(llm.validate_answer(exam, payload, {'status': 'ok', 'words': ['all our', 'work']}), 'all our,work')
        self.assertIsNone(llm.validate_answer(exam, payload, {'status': 'ok', 'words': ['invented', 'work']}))

    def test_disabled_makes_no_request(self):
        with patch.object(llm, 'load_config', return_value=None), patch.object(llm.requests, 'Session') as session:
            self.assertIsNone(llm.solve(self.info))
            session.assert_not_called()

    def test_success_cache_and_request_privacy(self):
        session = self.call_model('{"status":"ok","choice_id":"0"}')
        with patch.object(llm, 'load_config', return_value=self.config), patch.object(llm.requests, 'Session', return_value=session):
            self.assertEqual(llm.solve(self.info), 7)
            self.assertEqual(llm.solve(self.info), 7)
        post = session.__enter__.return_value.post
        self.assertEqual(post.call_count, 1)
        kwargs = post.call_args.kwargs
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('SECRET', json.dumps(kwargs['json']))

    def test_deepseek_parameters_and_cache_isolation(self):
        session = self.call_model('{"status":"ok","choice_id":"0"}')
        config = dict(self.config)
        with patch.object(llm, 'load_config', return_value=config), patch.object(llm.requests, 'Session', return_value=session):
            self.assertEqual(llm.solve(self.info), 7)
            body = session.__enter__.return_value.post.call_args.kwargs['json']
            self.assertNotIn('thinking', body)
            self.assertNotIn('response_format', body)
            config['provider'] = 'deepseek'
            config['model'] = 'deepseek-flash'
            self.assertEqual(llm.solve(self.info), 7)
            body = session.__enter__.return_value.post.call_args.kwargs['json']
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(body['model'], 'deepseek-flash')
            self.assertEqual(session.__enter__.return_value.post.call_count, 2)

    def test_failures_skip(self):
        for content, status, finish in [('not json', 200, 'stop'), ('   ', 200, 'stop'), ('{}', 429, 'stop'),
                ('{"status":"unknown"}', 200, 'stop'), ('{}', 200, 'length')]:
            with self.subTest(content=content, status=status, finish=finish):
                session = self.call_model(content, status, finish)
                with patch.object(llm, 'load_config', return_value=self.config), patch.object(llm.requests, 'Session', return_value=session):
                    self.assertIsNone(llm.solve(self.info))
        with patch.object(llm, 'load_config', return_value=self.config), patch.object(llm.requests, 'Session', side_effect=TimeoutError):
            self.assertIsNone(llm.solve(self.info))

    def test_config_key_only_from_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            config = {'enabled': True, 'base_url': 'https://provider.example/v1', 'model': 'test'}
            path.write_text(json.dumps(config))
            with patch.object(llm, 'CONFIG_PATH', path), patch.dict('os.environ', {'CIDAREN_LLM_API_KEY': 'test-key'}):
                self.assertEqual(llm.load_config()['key'], 'test-key')
                config['base_url'] = 'http://provider.example/v1'
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    llm.load_config()

    def test_audio_and_unknown_modes_not_sent(self):
        for mode in [11, 13, 21, 22, 31, 999]:
            self.assertIsNone(llm.build_payload(dict(self.exam, topic_mode=mode), {}))


class DispatchTests(unittest.TestCase):
    def test_rule_answer_bypasses_model_and_none_calls_model(self):
        # Compile the actual dispatcher without loading GUI/spaCy/platform clients.
        source = Path('answer_questions/answer_questions.py').read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'answer')
        namespace = {'select_word': MagicMock(return_value='rule-answer'),
                     'solve_with_llm': MagicMock(return_value='model-answer'),
                     'query_answer': SimpleNamespace(logger=MagicMock())}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<dispatcher>', 'exec'), namespace)
        self.assertEqual(namespace['answer'](object(), 32), 'rule-answer')
        namespace['solve_with_llm'].assert_not_called()
        namespace['select_word'].return_value = None
        self.assertEqual(namespace['answer'](object(), 32), 'model-answer')
        namespace['solve_with_llm'].assert_called_once()
        namespace['solve_with_llm'].return_value = None
        self.assertIsNone(namespace['answer'](object(), 32))


if __name__ == '__main__':
    unittest.main()
