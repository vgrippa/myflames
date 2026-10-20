"""Read projections must never carry an executable write into capture."""
import unittest
from myflames.analysis_query import prepare
from myflames.live import bind_query

class TestReadProjection(unittest.TestCase):
    def test_supported_dml(self):
        cases = {
            'UPDATE `app`.`orders` SET status=2 WHERE id=1 ORDER BY id LIMIT 3;': 'SELECT * FROM `app`.`orders` WHERE id=1 ORDER BY id LIMIT 3',
            "UPDATE orders AS o SET v=(SELECT max(v) FROM q) WHERE o.id=:id": 'SELECT * FROM orders AS o WHERE o.id=:id',
            'DELETE FROM orders WHERE id=?': 'SELECT * FROM orders WHERE id=?',
            'INSERT INTO archive (id) SELECT id FROM orders WHERE id=2': 'SELECT id FROM orders WHERE id=2',
            'REPLACE INTO archive SELECT * FROM orders': 'SELECT * FROM orders',
            'EXPLAIN ANALYZE UPDATE orders SET v=3 WHERE id=1': 'SELECT * FROM orders WHERE id=1',
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                result=prepare(original)
                self.assertEqual(result['problem'],'')
                self.assertTrue(result['converted'])
                self.assertEqual(result['sql'],expected)
                self.assertIn('original statement can change data',result['warning'])
                self.assertIn('Write cost',result['warning'])
                bind_query(result['sql'],{'id':1})

    def test_select_unchanged(self):
        for sql in ('SELECT 1', 'WITH x AS (SELECT 1) SELECT * FROM x'):
            self.assertEqual(prepare(sql),dict(sql=sql, converted=False, warning='', problem=''))
        self.assertEqual(prepare('EXPLAIN FORMAT=JSON SELECT 1')['sql'],'SELECT 1')

    def test_no_unsafe_fallback(self):
        for sql in ('TRUNCATE TABLE t', 'LOAD DATA INFILE \'x\' INTO TABLE t',
                    'INSERT INTO t VALUES (1)', 'INSERT INTO t SET n=1',
                    'DELETE t FROM t JOIN u ON t.id=u.id',
                    'UPDATE t JOIN u ON t.id=u.id SET t.n=1',
                    'UPDATE t SET n=1; DELETE FROM u',
                    'UPDATE t SET n=1 WHERE GET_LOCK(\'x\',0)',
                    'DELETE FROM t WHERE mydb.mutating_function(id)',
                    'INSERT INTO t SELECT * FROM u ON DUPLICATE KEY UPDATE n=1',
                    'SELECT * INTO OUTFILE \'x\' FROM t',
                    'UPDATE t SET n=1 /*!; DROP TABLE t */',
                    'UPDATE t SET n=1 WHERE id=(SELECT 1',
                    'WITH x AS (SELECT 1) DELETE FROM t',
                    'UPDATE t SET n=1 WHERE id=1 --\x01 x',
                    'UPDATE t SET nonsense WHERE id=7', 'INSERT INTO t SELECT id FROM s RETURNING id',
                    'SELECT SLEEP(1)', 'EXPLAIN ANALYZE TRUNCATE TABLE t',
                    "DELETE FROM t WHERE v='\\'; DROP TABLE t; -- '"):
            with self.subTest(sql=sql):
                result=prepare(sql)
                self.assertTrue(result['problem'])
                self.assertEqual(result['sql'],'')
                self.assertFalse(result['converted'])

    def test_keywords_inside_quotes_not_structural(self):
        result=prepare("UPDATE `set` SET v='where DELETE FROM t' WHERE id=2")
        self.assertEqual(result['sql'],'SELECT * FROM `set` WHERE id=2')
        self.assertFalse(result['problem'])

    def test_input_bounds(self):
        for value in (None, {}, '', 'SELECT '+('x'*200000)):
            self.assertTrue(prepare(value)['problem'])
