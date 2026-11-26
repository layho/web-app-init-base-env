import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import agent


class AgentTests(unittest.TestCase):
    def test_config_rejects_unsafe_credential_and_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("a" * 40)
            path.chmod(0o600)
            env = {"HOSTOPS_HOST_ID": "macmini-01", "HOSTOPS_GATEWAY_ORIGIN": "https://base.example",
                   "HOSTOPS_AGENT_TOKEN_FILE": str(path)}
            with patch.dict(os.environ, env):
                self.assertEqual(agent.config(), ("macmini-01", "https://base.example", "a" * 40))
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    agent.config()
                path.chmod(0o600)
                link = Path(directory) / "link"
                link.symlink_to(path)
                with patch.dict(os.environ, {"HOSTOPS_AGENT_TOKEN_FILE": str(link)}):
                    with self.assertRaises(OSError):
                        agent.config()
                for origin in ("http://base.example", "https://user@base.example", "https://base.example/path"):
                    with patch.dict(os.environ, {"HOSTOPS_GATEWAY_ORIGIN": origin}):
                        with self.assertRaises(ValueError):
                            agent.config()

    def test_heartbeat_request_and_ack(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"accepted":true,"schema":1}')
        with patch.object(agent, "heartbeat_payload", return_value={"host_id": "macmini-01"}):
            agent.send_heartbeat("macmini-01", "https://base.example", "a"*40, opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://base.example/api/hostops/v1/agent/heartbeat")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"host_id": "macmini-01"})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + "a"*40)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)

    def test_bad_ack_rejected(self):
        for raw in (b'{"accepted":false,"schema":1}', b'x'*4097):
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = io.BytesIO(raw)
            with patch.object(agent, "heartbeat_payload", return_value={}):
                with self.assertRaises(ValueError):
                    agent.send_heartbeat("macmini-01", "https://base.example", "a"*40, opener)

    def test_no_redirect(self):
        self.assertIsNone(agent.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))
