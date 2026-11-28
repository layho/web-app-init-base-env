import copy
import hashlib
import json
import unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.control_authz.canonical import BODY_STRINGS, INT_FIELDS, SECRET_FIELDS, SCOPE_KEYS
from app.control_authz.proof import sign_request, checked_message, verify_signature
from app.control_authz.errors import AuthnError

class RealEd25519Test(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        doc = {k: '' for k in BODY_STRINGS + tuple(SECRET_FIELDS)}
        doc.update({k: 0 for k in INT_FIELDS})
        doc.update(schema_version=2, operation='feedback.write', authorizer='fixture', repo_id='repo', task_id='tsk_w01', app_id='base', target_app_id='base', producer_id='fixture', stream_id='fixture:task', object_id='fb_1', phase='feedback.submit', action='feedback.submit', result=hashlib.sha256(json.dumps({'body':'测试'}, ensure_ascii=False).encode()).hexdigest(), policy_revision=1, credential_scope='feedback', credential_generation=1, idempotency_key='idem', method='POST', path='/isolated/w05/feedback')
        doc['scope'] = dict(zip(SCOPE_KEYS, (doc['repo_id'],doc['task_id'],doc['app_id'],doc['credential_scope'],doc['phase'],doc['object_id'])))
        self.signed = sign_request(self.key, signer_id='fixture', key_id='fixture-key', operation='feedback.write', body=doc, declared_kind='test', declared_id='fixture', declared_proof_ref='test-proof://fixture', issued_at=100, expires_at=200, nonce='nonce')
    def verify(self, signed, pub=None):
        msg=checked_message('feedback.write', signed, signed['proof'])
        verify_signature(pub or self.pub, signed['proof']['signature'], msg)
    def test_valid(self): self.verify(self.signed)
    def test_changed_body_digest(self):
        altered=copy.deepcopy(self.signed); altered['result']='0'*64
        with self.assertRaises(AuthnError): self.verify(altered)
    def test_changed_task_and_scope(self):
        altered=copy.deepcopy(self.signed); altered['task_id']=altered['scope']['task_id']='foreign'
        with self.assertRaises(AuthnError): self.verify(altered)
    def test_changed_actor(self):
        altered=copy.deepcopy(self.signed); altered['actor']['id']='other'
        with self.assertRaises(AuthnError): self.verify(altered)
    def test_wrong_public_key(self):
        wrong=Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        with self.assertRaises(AuthnError): self.verify(self.signed, wrong)
    def test_changed_nonce(self):
        altered=copy.deepcopy(self.signed); altered['proof']['nonce']='other'
        with self.assertRaises(AuthnError): self.verify(altered)
    def test_changed_signature(self):
        altered=copy.deepcopy(self.signed); altered['proof']['signature']='00'*64
        with self.assertRaises(AuthnError): self.verify(altered)
    def test_changed_proof_declared_fields(self):
        for field in ('declared_kind','declared_id','declared_proof_ref'):
            with self.subTest(field=field):
                altered=copy.deepcopy(self.signed); altered['proof'][field]='other'
                with self.assertRaises(AuthnError): self.verify(altered)
