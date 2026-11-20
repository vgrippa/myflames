"""CSV apply keeps real file checks, policy, review tokens and login guards."""
from unittest import mock

import test_csv_import_api as csv_tests
from myflames import connector, csv_import_api, editor_session
from myflames.connection_groups import ProfileReadError
from myflames.workspace_store import ENDPOINT_FIELDS


OPEN_SESSION = csv_import_api._open_session
ACTOR = csv_import_api._actor


class ImportGroupConsentTests(csv_tests.Base):
    def setUp(self):
        super(ImportGroupConsentTests, self).setUp()
        self.profile = self.ws.store.save('profile', dict(name='member', group='G', type='development',
            host='db.internal', port=3306, user='admin'), 'member', password='conn-secret', remember=True)
        self.connection = dict({key: self.profile.get(key) for key in ENDPOINT_FIELDS},
                               profile_id='member', group_name='G')

    def race(self, method, remove):
        plan = self.plan(method=method)
        self.assertIsNotNone(plan['token'], plan)
        before = len(self.server.sent)
        def features(*args, **kwargs):
            if remove:
                self.ws.store.assign_profile_group(['member'], '')
            else:
                self.ws.store.set_profile_safety('member', read_only=True)
            return {}

        # Planning's synthetic server supplied the exact file/table review.
        # Apply uses the real private EditorSession up to its process boundary.
        with mock.patch.object(csv_import_api, '_open_session', OPEN_SESSION), \
                mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(editor_session, '_client_features', side_effect=features), \
                mock.patch.object(editor_session.subprocess, 'Popen') as spawn:
            with self.assertRaises(ProfileReadError):
                self.apply(plan)
        spawn.assert_not_called()
        self.assertEqual(len(self.server.sent), before)
        record = self.audits()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'), record)

    def test_insert_member_removed_after_credentials_sends_nothing(self):
        self.race('insert', True)

    def test_insert_read_only_after_credentials_sends_nothing(self):
        self.race('insert', False)

    def test_load_data_member_removed_after_credentials_sends_nothing(self):
        self.race('load_data', True)

    def test_load_data_read_only_after_credentials_sends_nothing(self):
        self.race('load_data', False)

    def test_privilege_helper_preserves_consent_refusal(self):
        # This also exercises the persistent session's subsequent client
        # logins, rather than just its first Popen.
        payload = dict(connection=self.connection)
        options, identity, safety, _label = editor_session._connection_facts(payload, self.ws.store)
        full = editor_session._group_options(options, payload, self.ws.store, identity, safety,
                                              statements=['INSERT INTO db.t VALUES (...)'], operation='import_csv')
        from types import SimpleNamespace
        session = SimpleNamespace(_conn=connector.MySQLConnection(**options))
        session._conn._login_guard = full['_login_guard']
        self.addCleanup(session._conn.close)
        self.ws.store.set_profile_safety('member', read_only=True)
        # Base replaces the actor for its synthetic planning replies. Here
        # invoke the module's real implementation instead.
        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector, '_run_bounded') as spawn:
            with self.assertRaises(ProfileReadError):
                ACTOR(session)
        spawn.assert_not_called()
        record = self.audits()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))
