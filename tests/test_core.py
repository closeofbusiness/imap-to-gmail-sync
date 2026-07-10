"""Unit tests for imap_to_gmail_sync.core — mocked IMAP/Gmail, no network."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_to_gmail_sync.core import (
    SyncConfig,
    extract_message_id,
    gmail_has_message_id,
    import_rfc822_to_gmail,
    load_config_from_env,
    load_state,
    move_messages_to_folder,
    save_state,
    run_sync,
    verify_gmail_import,
)


class TestExtractMessageId(unittest.TestCase):
    def test_reads_message_id_header(self):
        raw = (
            b"From: a@example.com\r\n"
            b"To: b@example.com\r\n"
            b"Message-ID: <abc@example.com>\r\n"
            b"\r\nbody"
        )
        self.assertEqual(extract_message_id(raw), "<abc@example.com>")


class TestConfig(unittest.TestCase):
    def test_disabled_when_flag_off(self):
        with mock.patch.dict(os.environ, {"IMAP2GMAIL_SYNC_ENABLED": "0"}, clear=True):
            self.assertIsNone(load_config_from_env())

    def test_loads_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "IMAP2GMAIL_SYNC_ENABLED": "1",
                "IMAP2GMAIL_SRC_HOST": "mail.example.com",
                "IMAP2GMAIL_SRC_USER": "user@example.com",
                "IMAP2GMAIL_SRC_PASS": "secret",
                "IMAP2GMAIL_STATE_DIR": tmp,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = load_config_from_env()
            self.assertIsNotNone(cfg)
            assert cfg is not None
            self.assertEqual(cfg.src_host, "mail.example.com")
            self.assertEqual(cfg.gmail_label, "Imported")
            self.assertIsNone(cfg.synced_folder)

    def test_synced_folder_optional_and_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "IMAP2GMAIL_SYNC_ENABLED": "1",
                "IMAP2GMAIL_SRC_HOST": "mail.example.com",
                "IMAP2GMAIL_SRC_USER": "user@example.com",
                "IMAP2GMAIL_SRC_PASS": "secret",
                "IMAP2GMAIL_STATE_DIR": tmp,
                "IMAP2GMAIL_SYNCED_FOLDER": "Synced",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = load_config_from_env()
            assert cfg is not None
            self.assertEqual(cfg.synced_folder, "Synced")


class TestState(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, {"last_uid": 42, "uidvalidity": 1})
            loaded = load_state(path)
            self.assertEqual(loaded["last_uid"], 42)


class TestRunSync(unittest.TestCase):
    def test_dry_run_counts_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "user.json"
            cfg = SyncConfig(
                account_id="user",
                src_host="mail.example.com",
                src_user="user@example.com",
                src_pass="x",
                src_folder="INBOX",
                gmail_label="Imported/Test",
                after_date=None,
                state_path=state_path,
            )
            raw = (
                b"From: a@example.com\r\n"
                b"Message-ID: <one@test>\r\n"
                b"\r\nhi"
            )
            with mock.patch(
                "imap_to_gmail_sync.core.fetch_new_messages",
                return_value=([(101, raw)], {"last_uid": 101, "uidvalidity": 9, "imported_message_ids": []}),
            ):
                result = run_sync(cfg, dry_run=True)
            self.assertEqual(result.fetched, 1)
            self.assertEqual(result.imported, 1)

    def test_skips_duplicate_message_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "user.json"
            cfg = SyncConfig(
                account_id="user",
                src_host="mail.example.com",
                src_user="user@example.com",
                src_pass="x",
                src_folder="INBOX",
                gmail_label="Imported/Test",
                after_date=None,
                state_path=state_path,
            )
            raw = (
                b"From: a@example.com\r\n"
                b"Message-ID: <dup@test>\r\n"
                b"\r\nhi"
            )
            with mock.patch(
                "imap_to_gmail_sync.core.fetch_new_messages",
                return_value=([(5, raw)], {"last_uid": 0, "uidvalidity": 1, "imported_message_ids": []}),
            ), mock.patch("imap_to_gmail_sync.core._gmail_access_token", return_value="tok"), mock.patch(
                "imap_to_gmail_sync.core.ensure_gmail_label", return_value="Label_1"
            ), mock.patch("imap_to_gmail_sync.core.gmail_has_message_id", return_value=True):
                result = run_sync(cfg, dry_run=False)
            self.assertEqual(result.skipped_duplicate, 1)
            self.assertEqual(result.imported, 0)


class TestImportIncludesInbox(unittest.TestCase):
    def test_import_labels_include_inbox(self):
        """Imported mail must carry INBOX alongside the custom label, or it's
        invisible in the primary inbox list — Gmail only shows a message in
        the default inbox view if it carries the INBOX label."""
        with mock.patch("imap_to_gmail_sync.core._gmail_request") as req:
            req.return_value = {"id": "gmail123"}
            import_rfc822_to_gmail("tok", "Label_49", b"From: a@example.com\r\n\r\nbody")
            req.assert_called_once()
            json_body = req.call_args.kwargs.get("json_body") or req.call_args[1].get("json_body")
            self.assertIn("INBOX", json_body["labelIds"])
            self.assertIn("Label_49", json_body["labelIds"])


class TestVerifyGmailImport(unittest.TestCase):
    def test_returns_true_when_id_matches(self):
        with mock.patch("imap_to_gmail_sync.core._gmail_request") as req:
            req.return_value = {"id": "gmail123"}
            self.assertTrue(verify_gmail_import("tok", "gmail123"))

    def test_returns_false_on_id_mismatch(self):
        with mock.patch("imap_to_gmail_sync.core._gmail_request") as req:
            req.return_value = {"id": "someone-else"}
            self.assertFalse(verify_gmail_import("tok", "gmail123"))

    def test_returns_false_on_http_error(self):
        import httpx

        with mock.patch("imap_to_gmail_sync.core._gmail_request") as req:
            req.side_effect = httpx.HTTPStatusError("nope", request=mock.Mock(), response=mock.Mock())
            self.assertFalse(verify_gmail_import("tok", "gmail123"))


class TestMoveMessagesToFolder(unittest.TestCase):
    def _mock_conn(self):
        conn = mock.Mock()
        conn.login.return_value = ("OK", [b""])
        conn.create.return_value = ("OK", [b""])
        conn.select.return_value = ("OK", [b"1"])
        conn.uid.return_value = ("OK", [b""])
        conn.expunge.return_value = ("OK", [b""])
        conn.logout.return_value = ("OK", [b""])
        return conn

    def test_no_op_when_no_synced_folder(self):
        cfg = SyncConfig(
            account_id="u", src_host="h", src_user="u@h", src_pass="x",
            src_folder="INBOX", gmail_label="L", after_date=None,
            state_path=Path("/tmp/whatever.json"), synced_folder=None,
        )
        moved, errors = move_messages_to_folder(cfg, [1, 2, 3])
        self.assertEqual(moved, 0)
        self.assertEqual(errors, [])

    def test_moves_verified_uids_via_copy_delete_expunge(self):
        cfg = SyncConfig(
            account_id="u", src_host="h", src_user="u@h", src_pass="x",
            src_folder="INBOX", gmail_label="L", after_date=None,
            state_path=Path("/tmp/whatever.json"), synced_folder="Synced",
        )
        conn = self._mock_conn()
        with mock.patch("imap_to_gmail_sync.core.imaplib.IMAP4_SSL", return_value=conn):
            moved, errors = move_messages_to_folder(cfg, [5, 6])
        self.assertEqual(moved, 2)
        self.assertEqual(errors, [])
        conn.create.assert_called_once_with('"Synced"')
        conn.select.assert_called_once_with('"INBOX"', readonly=False)
        copy_calls = [c for c in conn.uid.call_args_list if c.args[0] == "copy"]
        store_calls = [c for c in conn.uid.call_args_list if c.args[0] == "store"]
        self.assertEqual(len(copy_calls), 2)
        self.assertEqual(len(store_calls), 2)
        conn.expunge.assert_called_once()

    def test_copy_failure_does_not_mark_deleted(self):
        cfg = SyncConfig(
            account_id="u", src_host="h", src_user="u@h", src_pass="x",
            src_folder="INBOX", gmail_label="L", after_date=None,
            state_path=Path("/tmp/whatever.json"), synced_folder="Synced",
        )
        conn = self._mock_conn()
        conn.uid.return_value = ("NO", [b"copy failed"])
        with mock.patch("imap_to_gmail_sync.core.imaplib.IMAP4_SSL", return_value=conn):
            moved, errors = move_messages_to_folder(cfg, [7])
        self.assertEqual(moved, 0)
        self.assertEqual(len(errors), 1)
        self.assertIn("COPY", errors[0])


class TestRunSyncMovesOnlyVerified(unittest.TestCase):
    def test_move_called_only_for_verified_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "user.json"
            cfg = SyncConfig(
                account_id="user",
                src_host="mail.example.com",
                src_user="user@example.com",
                src_pass="x",
                src_folder="INBOX",
                gmail_label="Imported/Test",
                after_date=None,
                state_path=state_path,
                synced_folder="Synced",
            )
            raw = b"From: a@example.com\r\nMessage-ID: <one@test>\r\n\r\nhi"
            with mock.patch(
                "imap_to_gmail_sync.core.fetch_new_messages",
                return_value=([(101, raw)], {"last_uid": 101, "uidvalidity": 9, "imported_message_ids": []}),
            ), mock.patch("imap_to_gmail_sync.core._gmail_access_token", return_value="tok"), mock.patch(
                "imap_to_gmail_sync.core.ensure_gmail_label", return_value="Label_1"
            ), mock.patch(
                "imap_to_gmail_sync.core.gmail_has_message_id", return_value=False
            ), mock.patch(
                "imap_to_gmail_sync.core.import_rfc822_to_gmail", return_value="gmail_abc"
            ), mock.patch(
                "imap_to_gmail_sync.core.verify_gmail_import", return_value=True
            ), mock.patch(
                "imap_to_gmail_sync.core.move_messages_to_folder", return_value=(1, [])
            ) as move_mock:
                result = run_sync(cfg, dry_run=False)
            self.assertEqual(result.imported, 1)
            self.assertEqual(result.moved, 1)
            move_mock.assert_called_once_with(cfg, [101])

    def test_move_not_called_when_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "user.json"
            cfg = SyncConfig(
                account_id="user",
                src_host="mail.example.com",
                src_user="user@example.com",
                src_pass="x",
                src_folder="INBOX",
                gmail_label="Imported/Test",
                after_date=None,
                state_path=state_path,
                synced_folder="Synced",
            )
            raw = b"From: a@example.com\r\nMessage-ID: <two@test>\r\n\r\nhi"
            with mock.patch(
                "imap_to_gmail_sync.core.fetch_new_messages",
                return_value=([(102, raw)], {"last_uid": 102, "uidvalidity": 9, "imported_message_ids": []}),
            ), mock.patch("imap_to_gmail_sync.core._gmail_access_token", return_value="tok"), mock.patch(
                "imap_to_gmail_sync.core.ensure_gmail_label", return_value="Label_1"
            ), mock.patch(
                "imap_to_gmail_sync.core.gmail_has_message_id", return_value=False
            ), mock.patch(
                "imap_to_gmail_sync.core.import_rfc822_to_gmail", return_value="gmail_xyz"
            ), mock.patch(
                "imap_to_gmail_sync.core.verify_gmail_import", return_value=False
            ), mock.patch(
                "imap_to_gmail_sync.core.move_messages_to_folder"
            ) as move_mock:
                result = run_sync(cfg, dry_run=False)
            self.assertEqual(result.imported, 1)
            self.assertEqual(result.moved, 0)
            move_mock.assert_not_called()


class TestGmailHasMessageId(unittest.TestCase):
    def test_query_strips_brackets(self):
        with mock.patch("imap_to_gmail_sync.core._gmail_request") as req:
            req.return_value = {"messages": [{"id": "1"}]}
            self.assertTrue(gmail_has_message_id("tok", "<abc@x>"))
            req.assert_called_once()
            params = req.call_args.kwargs.get("params") or req.call_args[1].get("params")
            self.assertIn("rfc822msgid:abc@x", params["q"])


if __name__ == "__main__":
    unittest.main()
