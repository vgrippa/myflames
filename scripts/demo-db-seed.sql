-- scripts/demo-db-seed.sql
--
-- Large, skewed seed data for the demo database started by scripts/demo-db.sh.
-- The schema comes from scripts/generate-fixtures.sh; this file only fills it.
--
--   categories      200      two-level hierarchy
--   users       200,000      country skewed: US 35% ... NZ 1%
--   products     50,000      category skewed toward low ids, 10% out of stock
--   orders    2,000,000      low user ids order far more often; a few
--                            hundred users have none.
--                            status: delivered 70%, cancelled 4%
--   order_items 6,000,000    popular products appear far more often
--   reviews   1,000,000      ratings skewed toward 5 stars
--
-- Values come from CRC32 of the row number, so every load is identical.

SET SESSION sql_log_bin = 0;
SET SESSION unique_checks = 0;
SET SESSION foreign_key_checks = 0;
SET GLOBAL innodb_flush_log_at_trx_commit = 2;

USE testdb;

TRUNCATE TABLE reviews;
TRUNCATE TABLE order_items;
TRUNCATE TABLE orders;
TRUNCATE TABLE products;
TRUNCATE TABLE users;
TRUNCATE TABLE categories;

-- Row-number helper: 1 .. 1,000,000
DROP TABLE IF EXISTS seq;
CREATE TABLE seq (n INT PRIMARY KEY);
INSERT INTO seq (n)
SELECT 1 + a.d + b.d * 10 + c.d * 100 + d.d * 1000 + e.d * 10000 + f.d * 100000
FROM (SELECT 0 d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) a,
     (SELECT 0 d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) b,
     (SELECT 0 d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) c,
     (SELECT 0 d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) d,
     (SELECT 0 d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) e,
     (SELECT 0 d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4
      UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) f;

-- categories: 20 top-level, 180 children
INSERT INTO categories (id, name, parent_id)
SELECT n, CONCAT('Category-', n), IF(n > 20, 1 + MOD(n, 20), NULL)
FROM seq WHERE n <= 200;

-- users
INSERT INTO users (id, name, email, country, created_at)
SELECT n,
       CONCAT('User ', n),
       CONCAT('user', n, '@example.com'),
       CASE
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 35 THEN 'US'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 50 THEN 'UK'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 62 THEN 'DE'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 72 THEN 'FR'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 80 THEN 'JP'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 87 THEN 'BR'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 93 THEN 'IN'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 97 THEN 'CA'
         WHEN MOD(CRC32(CONCAT('uc', n)), 100) < 99 THEN 'AU'
         ELSE 'NZ'
       END,
       DATE_ADD('2018-01-01', INTERVAL MOD(CRC32(CONCAT('ud', n)), 2900) DAY)
FROM seq WHERE n <= 200000;

-- products: category skewed toward low ids (squared uniform)
INSERT INTO products (id, name, category_id, price, stock, created_at)
SELECT n,
       CONCAT('Product-', n),
       1 + FLOOR(POW(CRC32(CONCAT('pc', n)) / 4294967296, 2) * 200),
       ROUND(1 + POW(CRC32(CONCAT('pp', n)) / 4294967296, 2) * 1999, 2),
       IF(MOD(CRC32(CONCAT('ps', n)), 10) = 0, 0, MOD(CRC32(CONCAT('pq', n)), 1000)),
       DATE_ADD('2019-01-01', INTERVAL MOD(CRC32(CONCAT('pd', n)), 2400) DAY)
FROM seq WHERE n <= 50000;

-- orders: user skewed (cubed uniform: low user ids order most)
INSERT INTO orders (id, user_id, status, total, created_at)
SELECT s.n + k.k * 1000000,
       1 + FLOOR(POW(CRC32(CONCAT('ou', s.n + k.k * 1000000)) / 4294967296, 3) * 200000),
       CASE
         WHEN MOD(CRC32(CONCAT('os', s.n + k.k * 1000000)), 100) < 70 THEN 'delivered'
         WHEN MOD(CRC32(CONCAT('os', s.n + k.k * 1000000)), 100) < 82 THEN 'shipped'
         WHEN MOD(CRC32(CONCAT('os', s.n + k.k * 1000000)), 100) < 90 THEN 'processing'
         WHEN MOD(CRC32(CONCAT('os', s.n + k.k * 1000000)), 100) < 96 THEN 'pending'
         ELSE 'cancelled'
       END,
       ROUND(5 + POW(CRC32(CONCAT('ot', s.n + k.k * 1000000)) / 4294967296, 2) * 2995, 2),
       DATE_ADD('2022-01-01 00:00:00',
                INTERVAL MOD(CRC32(CONCAT('od', s.n + k.k * 1000000)), 126144000) SECOND)
FROM seq s, (SELECT 0 k UNION ALL SELECT 1) k;

-- order_items: product skewed (squared uniform)
INSERT INTO order_items (id, order_id, product_id, quantity, unit_price)
SELECT s.n + k.k * 1000000,
       1 + MOD(CRC32(CONCAT('io', s.n + k.k * 1000000)), 2000000),
       1 + FLOOR(POW(CRC32(CONCAT('ip', s.n + k.k * 1000000)) / 4294967296, 2) * 50000),
       1 + MOD(CRC32(CONCAT('iq', s.n + k.k * 1000000)), 10),
       ROUND(1 + MOD(CRC32(CONCAT('iu', s.n + k.k * 1000000)), 199900) / 100, 2)
FROM seq s,
     (SELECT 0 k UNION ALL SELECT 1 UNION ALL SELECT 2
      UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5) k;

-- reviews: rating 5 = 45%, 4 = 25%, 3 = 12%, 2 = 8%, 1 = 10%
INSERT INTO reviews (id, product_id, user_id, rating, body, created_at)
SELECT n,
       1 + FLOOR(POW(CRC32(CONCAT('rp', n)) / 4294967296, 2) * 50000),
       1 + MOD(CRC32(CONCAT('ru', n)), 200000),
       CASE
         WHEN MOD(CRC32(CONCAT('rr', n)), 100) < 45 THEN 5
         WHEN MOD(CRC32(CONCAT('rr', n)), 100) < 70 THEN 4
         WHEN MOD(CRC32(CONCAT('rr', n)), 100) < 82 THEN 3
         WHEN MOD(CRC32(CONCAT('rr', n)), 100) < 90 THEN 2
         ELSE 1
       END,
       CONCAT('Review ', n, ' of product ', 1 + MOD(n, 50000)),
       DATE_ADD('2022-06-01', INTERVAL MOD(CRC32(CONCAT('rd', n)), 1200) DAY)
FROM seq;

DROP TABLE seq;
SET GLOBAL innodb_flush_log_at_trx_commit = 1;
ANALYZE TABLE categories, users, products, orders, order_items, reviews;
