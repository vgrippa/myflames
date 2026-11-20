"""K3: first-run ORACLE decisions wait for the opened session's binding.

The planner, confirmation policy and transport framing are production code.
Only the subprocess client's server answers come from the existing fixture.
"""
import os
import unittest

import test_editor_session as fixtures
import test_editor_session_identity as identity_fixtures
from test_editor_session_j2 import SAFETY, bound_session
from myflames import editor_session as ed


DEFINITION = "CREATE PROCEDURE p() BEGIN SELECT 1; END"
COMPOUND = "BEGIN NOT ATOMIC SELECT 1; END"


def script(body):
    return "DELIMITER //\n" + body + "//\nDELIMITER ;"


class EditorPlanningK3Tests(unittest.TestCase):
    def test_no_session_plans_definitions_and_anonymous_compounds_provisionally(self):
        for body in (DEFINITION, COMPOUND):
            with self.subTest(body=body):
                plan = ed._plan_run({"sql": script(body)}, None)
                self.assertEqual([unit["send"] for unit in plan["units"]], [body])
                self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
        # Even without a custom delimiter, the ORACLE check must wait for
        # startup. Ordinary statement splitting still applies unchanged.
        plan = ed._plan_run({"sql": COMPOUND}, None)
        self.assertEqual([unit["send"] for unit in plan["units"]],
                         ["BEGIN NOT ATOMIC SELECT 1", "END"])
        self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")

    def test_provisional_mode_changes_do_not_restore_the_oracle_refusal(self):
        for prefix in ("SET sql_mode=@saved;", "SET sql_mode='ORACLE';",
                       "SET sql_mode='ORACLE'; SET sql_mode='';"):
            for body in (DEFINITION, COMPOUND):
                with self.subTest(prefix=prefix, body=body):
                    plan = ed._plan_run({"sql": prefix + "\n" + script(body)}, None)
                    self.assertEqual(plan["units"][-1]["send"], body)
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")

    def test_existing_unbound_sessions_still_refuse_both_constructs(self):
        for engine, version in (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB")):
            for failure in ("routed_startup", "failed_identity"):
                session = bound_session(engine, version, "")
                if failure == "routed_startup":
                    session._routed_startup = True
                else:
                    session.server_identity = {}
                self.assertIs(ed._lexer_session_context(session)["identity_bound"], False)
                for body in (DEFINITION, COMPOUND):
                    with self.subTest(engine=engine, failure=failure, body=body):
                        with self.assertRaisesRegex(ed.EditorError, "ORACLE"):
                            ed._plan_run({"sql": script(body)}, session)


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class EditorFirstRunK3ProcessTests(unittest.TestCase):
    engine = "mysql"
    setUp = fixtures.ProcessTests.setUp
    call = fixtures.ProcessTests.call
    packets = identity_fixtures.EditorIdentityTests.packets
    rewrite_client = identity_fixtures.EditorIdentityTests.rewrite_client

    def review_first_run(self, sql, tab):
        review = self.call("/api/editor/run", tab=tab, sql=sql)
        self.assertEqual(review["status"], "needs_confirmation", review)
        self.assertIsNone(self.registry.get(tab), "review must precede opening the client")
        self.assertIsNone(review["session"])
        return review["confirmation"]["token"]

    def test_first_mysql_run_sends_the_definition_once_after_confirmation(self):
        sql = script(DEFINITION)
        token = self.review_first_run(sql, "mysql-first")
        self.assertFalse((self.state / "packets.log").exists())
        result = self.call("/api/editor/run", tab="mysql-first", sql=sql,
                           confirmed=True, confirm_token=token)
        self.assertEqual(result["status"], "complete", result)
        session = self.registry.get("mysql-first")
        self.assertIs(ed._lexer_session_context(session)["identity_bound"], True)
        self.assertEqual(session.engine, "mysql")
        self.assertEqual(result["run"]["statements"][0]["text"], DEFINITION)
        sent = [packet[2] for packet in self.packets() if DEFINITION in packet[2]]
        self.assertEqual(len(sent), 1, "provisional review must authorize exactly one guarded send")
        self.assertIn("SET @myflames_target = ", sent[0])

    def test_first_oracle_run_refuses_both_constructs_after_connecting_without_sending(self):
        os.environ["FAKE_ENGINE"] = "mariadb"
        self.rewrite_client(lambda text: text.replace(
            'self.sql_mode = "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"',
            'self.sql_mode = "ORACLE"'))
        for index, body in enumerate((DEFINITION, COMPOUND)):
            tab = "oracle-first-%d" % index
            sql = script(body)
            token = self.review_first_run(sql, tab)
            result = self.call("/api/editor/run", tab=tab, sql=sql,
                               confirmed=True, confirm_token=token)
            self.assertEqual((result["status"], result.get("code")),
                             ("invalid", "invalid_statement"), result)
            self.assertIn("ORACLE", result["message"])
            session = self.registry.get(tab)
            self.assertEqual(session.engine, "mariadb")
            self.assertIn("ORACLE", session.sql_mode)
            self.assertIs(ed._lexer_session_context(session)["identity_bound"], True)
            self.assertFalse(any(body in packet[2] for packet in self.packets()),
                             "startup helpers may run, but refused user SQL must never reach the client")
            self.assertNotIn("run", result)

    def test_bound_replan_policy_refusal_returns_before_starting_a_run(self):
        # Unknown-version planning includes BEGIN/END as a routine body.
        # MySQL skips these comments, exposing a second standalone DDL.
        body = ("CREATE PROCEDURE p() /*!999999 BEGIN */ SELECT 1; "
                "DROP TABLE must_not_run; /*!999999 END */")
        sql = "DELIMITER $$\n" + body + "$$\nDELIMITER ;"
        token = self.review_first_run(sql, "bound-refusal")
        self.assertFalse((self.state / "packets.log").exists())
        result = self.call("/api/editor/run", tab="bound-refusal", sql=sql,
                           confirmed=True, confirm_token=token)
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual(result["decision"]["action"], "refuse")
        self.assertIn("one at a time", result["message"])
        session = self.registry.get("bound-refusal")
        self.assertIs(ed._lexer_session_context(session)["identity_bound"], True)
        self.assertFalse(any(body in packet[2] for packet in self.packets()))
        self.assertNotIn("run", result)
        self.assertEqual(self.store.audit_records()[0]["outcome"], "ddl_policy")

    def test_existing_routed_session_refuses_both_constructs_without_another_packet(self):
        for engine in ("mysql", "mariadb"):
            tab = "routed-" + engine
            os.environ["FAKE_ENGINE"] = engine
            self.assertEqual(self.call("/api/editor/open", tab=tab)["status"], "idle")
            session = self.registry.get(tab)
            session._routed_startup = True
            before = len(self.packets())
            for body in (DEFINITION, COMPOUND):
                with self.subTest(engine=engine, body=body):
                    result = self.call("/api/editor/run", tab=tab, sql=script(body))
                    self.assertEqual(result["status"], "invalid", result)
                    self.assertIn("ORACLE", result["message"])
                    self.assertEqual(len(self.packets()), before)
                    self.assertIs(self.registry.get(tab), session)


if __name__ == "__main__":
    unittest.main()
