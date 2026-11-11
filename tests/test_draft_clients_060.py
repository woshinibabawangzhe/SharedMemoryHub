"""CLI identities, native provenance and unknown-write journals; no external requests."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch
import httpx

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from clients.draft_cli import main
from clients.memory_cli import call_hub


class DraftClients(unittest.TestCase):
    def setUp(self):
        self.runtime=ROOT/'receipts/drafts-060-20261110-091851/cli-synthetic'/uuid.uuid4().hex
        self.runtime.mkdir(parents=True)
        self.args=['--client','codex','--runtime',str(self.runtime),'--base-url','http://127.0.0.1:9999']

    def test_unknown_journal_and_native_session(self):
        payload={'request_key':'synthetic-key','base_draft_revision':2,'base_memory_revision':None}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'synthetic-native-context'}),patch('sys.stdin',io.StringIO(json.dumps(payload))),contextlib.redirect_stdout(io.StringIO()),patch('clients.draft_cli.call_hub',return_value={'status':'WRITE_RESULT_UNKNOWN','http_status':503}) as call:
            self.assertEqual(main(self.args+['suggest','--id','draft_fixture','--stdin']),2)
            self.assertEqual(call.call_args.kwargs['payload']['session_id'],'synthetic-native-context')
        saved=list((self.runtime/'client_requests/codex').glob('*.json'))
        self.assertEqual(len(saved),1)
        body=json.loads(saved[0].read_text(encoding='utf-8'));self.assertNotIn('token',body)
        changed={**payload,'reason':'changed'}
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'synthetic-native-context'}),patch('sys.stdin',io.StringIO(json.dumps(changed))),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):main(self.args+['suggest','--id','draft_fixture','--stdin'])

    def test_native_session_missing_stops_before_write(self):
        with patch.dict(os.environ,{},clear=True),patch('sys.stdin',io.StringIO('{"request_key":"key"}')),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):main(self.args+['suggest','--id','draft_fixture','--stdin'])
        self.assertFalse((self.runtime/'client_requests').exists())

    def test_write_timeout_classification_for_new_endpoint(self):
        tokens=self.runtime/'fixture-tokens.json';tokens.write_text(json.dumps({'CODEX_CLIENT_TOKEN':'synthetic-disposable-token'}))
        with patch('httpx.Client.request',side_effect=httpx.ReadTimeout('synthetic timeout')):
            result=call_hub('POST','/v1/drafts/draft_fixture/suggestions','codex',{'request_key':'fixed-key'},tokens_path=tokens)
        self.assertEqual((result['status'],result['request_key']),('WRITE_RESULT_UNKNOWN','fixed-key'))

    def test_identity_instance_and_local_host_required(self):
        for args in (['get','--id','draft_fixture'],['--client','codex','--runtime',str(self.runtime),'get','--id','draft_fixture'],['--client','codex','--runtime',str(self.runtime),'--base-url','https://example.com','get','--id','draft_fixture']):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):main(args)


if __name__=='__main__':unittest.main(verbosity=2)
