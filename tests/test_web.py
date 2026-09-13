from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dev_pipeline.cli import main
from dev_pipeline.notifications import Notifications
from dev_pipeline.store import WorkStore
from dev_pipeline.web_server import handler_for
from dev_pipeline.web_service import Conflict, WebService


FAKE_AGENT = r'''
import json, pathlib, shlex, sys, time
prompt = sys.stdin.read()
pathlib.Path('last-prompt.txt').write_text(prompt)
while pathlib.Path('slow').exists():
    time.sleep(0.05)
if prompt.startswith('Turn this human request'):
    result = {'description': '集計を作る',
              'phases': [{'goal': name, 'scope': ['implementation'], 'acceptance_criteria': ['tests pass']}
                         for name in ['集計の実装', '集計の拡張']]}
elif prompt.startswith('You are the implementation agent'):
    pathlib.Path('feature.py').write_text('def total(xs): return sum(xs)\n'.replace('\\n', '\n'))
    check = shlex.join([sys.executable, '-c', 'from feature import total; assert total([1, 2]) == 3; print("aggregation test passed")'])
    pathlib.Path('devpipeline-tests.json').write_text(json.dumps({'command': check}))
    result = {'result': 'GOAL_REACHED', 'completed': ['集計を実装'], 'remaining': [], 'problems': [], 'recommended_next_action': 'review'}
elif prompt.startswith('Create a concise Japanese summary'):
    result = {'recommendation': 'APPROVE', 'decision_reason': '確認済みのため承認できます',
              'affected_scope': '今回の集計機能', 'required_action': '承認してください', 'items': []}
else:
    result = {'result': 'PASS', 'findings': []}
print(json.dumps(result))
'''


class FakeWebService(WebService):
    def __init__(self, work_dir, program):
        self.program = program
        super().__init__(work_dir)

    def _base_command(self, job, config):
        return [sys.executable, '-m', 'dev_pipeline.cli', '--work-dir', str(self.work_dir),
                '--repo', config['repository'], '--agent', 'command', '--agent-command',
                shlex.join([sys.executable, str(self.program)])]


class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.program = self.root / 'agent.py'
        self.program.write_text(FAKE_AGENT)
        self.service = FakeWebService(self.root / 'work', self.program)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def wait_job(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = self.service.detail(job['initiative_id'])
            if current['job']['status'] not in ('queued', 'running', 'stopping'):
                return current
            time.sleep(.05)
        self.fail('job timed out')

    def test_real_cli_lifecycle_feedback_stale_approval_next_phase_and_completion(self):
        job = self.service.create({'id': 'TEST', 'request_id': 'create-1',
                                   'request': '集計を作る; $(touch should-not-exist) <script>alert(1)</script>'})
        self.assertEqual(job['id'], self.service.create({'request_id': 'create-1'})['id'])
        detail = self.wait_job(job)
        self.assertEqual('succeeded', detail['job']['status'], detail['job_log'])
        self.assertEqual('AWAITING_HUMAN_CHECK', detail['state']['state'])
        self.assertEqual('PASS', detail['evidence'][0]['verification']['overall'])
        self.assertIn('aggregation test passed', detail['evidence'][0]['verification_log'])
        self.assertEqual(1, detail['evidence'][0]['review']['run_id'])
        self.assertEqual('SUCCESS', detail['evidence'][0]['review_summary']['status'])
        self.assertEqual('APPROVE', detail['evidence'][0]['review_summary']['recommendation'])
        self.assertFalse((Path(detail['config']['repository']) / 'should-not-exist').exists())
        self.assertTrue((Path(detail['config']['repository']) / '.git').exists())
        old = detail['revision']
        job = self.service.act('TEST', 'human-check', {'request_id': 'fix-1', 'revision': old,
                                                     'result': 'FEEDBACK', 'feedback': '空の入力も確認してください'})
        detail = self.wait_job(job)
        self.assertEqual(2, detail['state']['current_run'])
        self.assertEqual('AWAITING_HUMAN_CHECK', detail['state']['state'])
        with self.assertRaises(Conflict):
            self.service.act('TEST', 'human-check', {'request_id': 'stale', 'revision': old, 'result': 'ACCEPT'})
        request = {'request_id': 'accept-1', 'revision': detail['revision'], 'result': 'ACCEPT'}
        job = self.service.act('TEST', 'human-check', request)
        detail = self.wait_job(job)
        self.assertEqual(job['id'], self.service.act('TEST', 'human-check', request)['id'])
        self.assertEqual('phase-02', detail['state']['current_phase'])
        self.assertEqual('AWAITING_HUMAN_CHECK', detail['state']['state'])
        self.assertEqual('accepted', detail['phases'][0]['status'])
        job = self.service.act('TEST', 'human-check', {'request_id': 'accept-2',
            'revision': detail['revision'], 'result': 'ACCEPT'})
        self.assertEqual('RESOLVED', self.wait_job(job)['state']['state'])

    def test_stop_planning_and_resume_without_duplicate_execution(self):
        repository = self.root / 'repo'; repository.mkdir()
        (repository / 'slow').touch()
        job = self.service.create({'id':'SLOW', 'request_id':'s1', 'request':'run', 'repository':str(repository)})
        with self.assertRaises(Conflict):
            self.service.act('SLOW','run',{'request_id':'duplicate'})
        # Wait until the agent really started, then exercise process-group shutdown.
        deadline = time.monotonic() + 5
        while not (repository / 'last-prompt.txt').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((repository / 'last-prompt.txt').exists())
        self.service.stop('SLOW')
        self.assertEqual('stopped', self.wait_job(job)['job']['status'])
        (repository / 'slow').unlink()
        resumed = self.service.act('SLOW','run',{'request_id':'resume'})
        self.assertEqual('AWAITING_HUMAN_CHECK', self.wait_job(resumed)['state']['state'])

    def test_settings_persist_and_service_is_single_instance(self):
        with self.assertRaises(Conflict):
            WebService(self.root / 'work')
        job = self.service.create({'id':'KEEP','request_id':'keep','request':'persist'})
        self.wait_job(job)
        self.service.close()
        self.service = FakeWebService(self.root / 'work', self.program)
        self.assertEqual('AWAITING_HUMAN_CHECK', self.service.detail('KEEP')['state']['state'])
        self.assertEqual(job['id'], self.service.detail('KEEP')['job']['id'])

    def test_detached_job_pid_saved_and_survives_service_restart(self):
        repository = self.root / 'repo_detach'; repository.mkdir()
        (repository / 'slow').touch()
        job = self.service.create({'id': 'DETACH', 'request_id': 'det1', 'request': 'run', 'repository': str(repository)})
        deadline = time.monotonic() + 5
        pid = None
        while time.monotonic() < deadline:
            current = self.service.detail('DETACH')
            pid = current['job'].get('pid')
            if pid:
                break
            time.sleep(0.05)
        self.assertIsNotNone(pid)
        self.assertGreater(pid, 0)
        self.assertTrue(WebService._is_pid_alive(pid))

        # Close the web service (simulating server restart)
        self.service.close()
        # Process MUST still be alive!
        self.assertTrue(WebService._is_pid_alive(pid))

        # Re-initialize service (re-attach)
        self.service = FakeWebService(self.root / 'work', self.program)
        restarted_detail = self.service.detail('DETACH')
        self.assertEqual('running', restarted_detail['job']['status'])
        self.assertEqual(pid, restarted_detail['job']['pid'])

        # Stop via new service
        self.service.stop('DETACH')
        detail = self.wait_job(restarted_detail['job'])
        self.assertEqual('stopped', detail['job']['status'])
        time.sleep(0.2)
        self.assertFalse(WebService._is_pid_alive(pid))

    def test_detached_job_finishes_successfully_after_service_restart(self):
        repository = self.root / 'repo_detach_done'; repository.mkdir()
        (repository / 'slow').touch()
        job = self.service.create({'id': 'DETACH_DONE', 'request_id': 'det2', 'request': 'run', 'repository': str(repository)})
        deadline = time.monotonic() + 5
        pid = None
        while time.monotonic() < deadline:
            current = self.service.detail('DETACH_DONE')
            pid = current['job'].get('pid')
            if pid:
                break
            time.sleep(0.05)
        self.assertIsNotNone(pid)

        # Close service while running
        self.service.close()

        # Remove slow file so the agent completes
        (repository / 'slow').unlink()
        deadline = time.monotonic() + 10
        while WebService._is_pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(WebService._is_pid_alive(pid))

        # Restart service: it should finalize the terminated job as succeeded
        self.service = FakeWebService(self.root / 'work', self.program)
        detail = self.service.detail('DETACH_DONE')
        self.assertEqual('succeeded', detail['job']['status'])
        self.assertEqual('AWAITING_HUMAN_CHECK', detail['state']['state'])

    def test_cli_lock_rejects_other_mutation_and_releases_after_exit(self):
        store = WorkStore(self.root / 'work', 'LOCK')
        with store.execution_lock(), patch('builtins.print'):
            self.assertEqual(2, main(['--work-dir',str(store.root),'run','LOCK']))
        with store.execution_lock():
            pass

    def test_cli_checks_revision_under_its_execution_lock(self):
        job = self.service.create({'id':'REV','request_id':'rev','request':'revision'})
        detail = self.wait_job(job)
        with patch('builtins.print'):
            self.assertEqual(2, main(['--work-dir',str(self.service.work_dir),'human-check','REV',
                'ACCEPT','--expected-revision','old']))
        self.assertEqual(detail['revision'], self.service.detail('REV')['revision'])

    def test_api_security_static_boundary_and_creation(self):
        server = ThreadingHTTPServer(('127.0.0.1',0), BaseHTTPRequestHandler)
        origin = f'http://127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = handler_for(self.service,{origin})
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(path, data=None, headers=None):
            request = Request(origin+path, data=json.dumps(data).encode() if data is not None else None,
                              headers=headers or {})
            try:
                with urlopen(request,timeout=5) as response:
                    return response.status, response.read(), response.headers
            except HTTPError as error:
                with error:
                    return error.code, error.read(), error.headers
        try:
            code, body, headers = request('/')
            self.assertEqual(200,code)
            self.assertIn('テスト'.encode(), body)
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
            code, body, _ = request('/app.js')
            self.assertEqual(200, code)
            self.assertIn('業務向け要約'.encode(), body)
            self.assertIn('技術的な詳細'.encode(), body)
            self.assertLess(body.index('businessReviewSummary'.encode()),
                            body.index('technicalFindings(result,!summary'.encode()))
            self.assertEqual(404, request('/work/REV/repo/feature.py')[0])
            self.assertEqual(400, request('/api/initiatives/..%2F..')[0])
            token = json.loads(request('/api/config')[1])['csrf_token']
            headers = {'Content-Type':'application/json','Origin':origin,'X-CSRF-Token':token}
            self.assertEqual(403, request('/api/initiatives',{}, {'Origin':origin})[0])
            self.assertEqual(403, request('/api/initiatives',{}, {**headers,'Origin':'https://evil.example'})[0])
            self.assertEqual(403, request('/',headers={'Host':'evil.example'})[0])
            code, body, _ = request('/api/initiatives',{'id':'HTTP','request':'create','request_id':'http'},headers)
            self.assertEqual(202,code,body)
            self.assertEqual('AWAITING_HUMAN_CHECK',self.wait_job(json.loads(body))['state']['state'])
        finally:
            server.shutdown();server.server_close();thread.join()


@unittest.skipUnless(importlib.util.find_spec('pywebpush'), 'install .[web] to test Web Push')
class NotificationTests(unittest.TestCase):
    def test_vapid_persistence_delivery_retry_expiry_and_ssrf_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            notifier = Notifications(Path(directory), 'mailto:test@example.com')
            key = notifier.public_key
            subscription = {'endpoint':'https://web.push.apple.com/Q123',
                            'keys':{'auth':'abc','p256dh':'def'}}
            with self.assertRaises(ValueError):
                notifier.subscribe({**subscription,'endpoint':'http://127.0.0.1/private'})
            notifier.subscribe(subscription)
            notifier.sender = Mock(side_effect=RuntimeError('temporary failure'))
            notifier.notify('承認待ち','TEST','TEST')
            notifier.deliver_pending()
            self.assertEqual(1,notifier.status()['pending'])
            self.assertTrue(notifier.status()['last_error'])
            notifier.sender = Mock()
            notifier.deliver_pending()
            self.assertEqual(0,notifier.status()['pending'])
            self.assertIsNone(notifier.status()['last_error'])
            payload = json.loads(notifier.sender.call_args.kwargs['data'])
            self.assertEqual('/#TEST',payload['url'])
            reloaded = Notifications(Path(directory),'mailto:test@example.com')
            self.assertEqual(key,reloaded.public_key)
            reloaded.unsubscribe(subscription['endpoint'])
            self.assertFalse(reloaded.data['subscriptions'])


if __name__ == '__main__':
    unittest.main()
