#!/usr/bin/env bash
# scripts/generate-mariadb-fixtures.sh
#
# Generates MariaDB ANALYZE FORMAT=JSON fixture files using Docker.
# Supports MariaDB 10.11 (LTS) and 11.4 (latest stable).
# Run once to populate test/fixtures/. Commit the resulting files.
#
# Usage:
#   ./scripts/generate-mariadb-fixtures.sh
#
# Requirements: Docker

set -euo pipefail

MYSQL_ROOT_PASSWORD="fixturepass"
MYSQL_DATABASE="testdb"
OUTPUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/test/fixtures"
FIXTURE_N=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { echo "[generate-mariadb-fixtures] $*"; }

mariadb_exec() {
  local container="$1"
  shift
  docker exec -i "$container" \
    mariadb -u root -p"$MYSQL_ROOT_PASSWORD" --silent "$@"
}

# Run an ANALYZE FORMAT=JSON and save output
run_analyze() {
  local container="$1"
  local tag="$2"
  local desc="$3"
  local sql="$4"
  FIXTURE_N=$((FIXTURE_N + 1))
  local fname
  fname=$(printf "%s/mariadb-%s-%03d-%s.json" "$OUTPUT_DIR" "$tag" "$FIXTURE_N" "$desc")
  docker exec -i "$container" \
    mariadb -u root -p"$MYSQL_ROOT_PASSWORD" \
    --raw --skip-column-names --silent \
    "$MYSQL_DATABASE" 2>/dev/null \
    -e "ANALYZE FORMAT=JSON $sql" \
    > "$fname"
  log "  [$FIXTURE_N] $desc -> $(basename "$fname")"
}

create_schema() {
  local container="$1"
  log "Creating schema and seeding data in $container..."
  mariadb_exec "$container" << 'SQL'
CREATE DATABASE IF NOT EXISTS testdb;
USE testdb;

-- Schema
CREATE TABLE categories (
  id       INT AUTO_INCREMENT PRIMARY KEY,
  name     VARCHAR(100) NOT NULL,
  parent_id INT NULL,
  INDEX idx_parent (parent_id)
);

CREATE TABLE users (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(100) NOT NULL,
  email      VARCHAR(200) NOT NULL,
  country    CHAR(2) NOT NULL,
  created_at DATE NOT NULL,
  UNIQUE KEY uq_email (email),
  INDEX idx_country (country),
  INDEX idx_created (created_at)
);

CREATE TABLE products (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(200) NOT NULL,
  category_id INT NOT NULL,
  price       DECIMAL(10,2) NOT NULL,
  stock       INT NOT NULL DEFAULT 0,
  created_at  DATE NOT NULL,
  INDEX idx_category (category_id),
  INDEX idx_price (price),
  INDEX idx_cat_price (category_id, price),
  INDEX idx_stock (stock)
);

CREATE TABLE orders (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  user_id    INT NOT NULL,
  status     VARCHAR(20) NOT NULL DEFAULT 'pending',
  total      DECIMAL(12,2) NOT NULL,
  created_at DATETIME NOT NULL,
  INDEX idx_user (user_id),
  INDEX idx_status (status),
  INDEX idx_created (created_at),
  INDEX idx_user_status (user_id, status)
);

CREATE TABLE order_items (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  order_id   INT NOT NULL,
  product_id INT NOT NULL,
  quantity   INT NOT NULL,
  unit_price DECIMAL(10,2) NOT NULL,
  INDEX idx_order (order_id),
  INDEX idx_product (product_id)
);

CREATE TABLE reviews (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  product_id INT NOT NULL,
  user_id    INT NOT NULL,
  rating     TINYINT NOT NULL,
  body       TEXT,
  created_at DATE NOT NULL,
  INDEX idx_product (product_id),
  INDEX idx_user (user_id),
  INDEX idx_prod_rating (product_id, rating)
);

-- Seed data using MariaDB sequence engine
INSERT INTO categories (name, parent_id)
SELECT CONCAT('Category-', seq),
       IF(seq > 10, 1 + FLOOR((seq-11)/5), NULL)
FROM seq_1_to_50;

INSERT INTO users (name, email, country, created_at)
SELECT CONCAT('User ', seq),
       CONCAT('user', seq, '@example.com'),
       ELT(1 + MOD(seq, 5), 'US','UK','DE','FR','JP'),
       DATE_ADD('2020-01-01', INTERVAL MOD(seq * 7, 1460) DAY)
FROM seq_1_to_3000;

INSERT INTO products (name, category_id, price, stock, created_at)
SELECT CONCAT('Product ', seq),
       1 + MOD(seq, 50),
       5.00 + MOD(seq * 3, 500),
       MOD(seq, 200),
       DATE_ADD('2021-01-01', INTERVAL MOD(seq * 3, 730) DAY)
FROM seq_1_to_1500;

INSERT INTO orders (user_id, status, total, created_at)
SELECT 1 + MOD(seq, 3000),
       ELT(1 + MOD(seq, 5), 'pending','processing','shipped','delivered','cancelled'),
       10.00 + MOD(seq * 7, 1000),
       DATE_ADD('2023-01-01', INTERVAL MOD(seq, 365) DAY)
FROM seq_1_to_10000;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT 1 + MOD(seq, 10000),
       1 + MOD(seq, 1500),
       1 + MOD(seq, 10),
       5.00 + MOD(seq * 3, 200)
FROM seq_1_to_25000;

INSERT INTO reviews (product_id, user_id, rating, body, created_at)
SELECT 1 + MOD(seq, 1500),
       1 + MOD(seq, 3000),
       1 + MOD(seq, 5),
       CONCAT('Review text for product ', 1 + MOD(seq, 1500)),
       DATE_ADD('2023-06-01', INTERVAL MOD(seq, 180) DAY)
FROM seq_1_to_5000;

ANALYZE TABLE categories, users, products, orders, order_items, reviews;
SQL
}

run_queries() {
  local container="$1"
  local tag="$2"
  FIXTURE_N=0

  # -- Table scans --
  run_analyze "$container" "$tag" "table-scan-no-filter" \
    "SELECT * FROM users"
  run_analyze "$container" "$tag" "table-scan-filter" \
    "SELECT * FROM users WHERE country = 'US'"
  run_analyze "$container" "$tag" "table-scan-products" \
    "SELECT * FROM products WHERE stock > 100"

  # -- PK lookups --
  run_analyze "$container" "$tag" "pk-lookup" \
    "SELECT * FROM users WHERE id = 42"
  run_analyze "$container" "$tag" "pk-lookup-product" \
    "SELECT * FROM products WHERE id = 100"

  # -- Index scans --
  run_analyze "$container" "$tag" "index-scan-country" \
    "SELECT country FROM users"
  run_analyze "$container" "$tag" "index-range-scan" \
    "SELECT * FROM products WHERE price BETWEEN 50 AND 100"
  run_analyze "$container" "$tag" "index-ref-lookup" \
    "SELECT * FROM orders WHERE user_id = 5"
  run_analyze "$container" "$tag" "covering-index" \
    "SELECT user_id FROM orders WHERE user_id = 5"

  # -- Filters --
  run_analyze "$container" "$tag" "filter-complex" \
    "SELECT * FROM users WHERE country = 'US' AND created_at > '2022-01-01'"

  # -- Sorts --
  run_analyze "$container" "$tag" "sort-simple" \
    "SELECT * FROM users ORDER BY name LIMIT 10"
  run_analyze "$container" "$tag" "sort-with-filter" \
    "SELECT * FROM orders WHERE status = 'pending' ORDER BY created_at DESC LIMIT 20"

  # -- Aggregates --
  run_analyze "$container" "$tag" "aggregate-count" \
    "SELECT country, COUNT(*) FROM users GROUP BY country"
  run_analyze "$container" "$tag" "aggregate-sum" \
    "SELECT user_id, SUM(total) FROM orders GROUP BY user_id HAVING SUM(total) > 500"

  # -- 2-table JOINs --
  run_analyze "$container" "$tag" "join-2t-inner" \
    "SELECT u.name, o.total FROM users u JOIN orders o ON o.user_id = u.id WHERE o.status = 'pending'"
  run_analyze "$container" "$tag" "join-2t-left" \
    "SELECT u.name, COUNT(o.id) cnt FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.id ORDER BY cnt DESC LIMIT 10"
  run_analyze "$container" "$tag" "join-2t-eq-ref" \
    "SELECT o.id, u.name FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id < 100"

  # -- 3-table JOINs --
  run_analyze "$container" "$tag" "join-3t" \
    "SELECT u.name, p.name, oi.quantity FROM users u JOIN orders o ON o.user_id = u.id JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id WHERE u.country = 'US' LIMIT 20"

  # -- 4-table JOINs --
  run_analyze "$container" "$tag" "join-4t-agg" \
    "SELECT u.country, c.name, SUM(oi.quantity) total_qty FROM users u JOIN orders o ON o.user_id = u.id JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id JOIN categories c ON c.id = p.category_id GROUP BY u.country, c.name ORDER BY total_qty DESC LIMIT 10"

  # -- Subqueries --
  run_analyze "$container" "$tag" "subquery-in" \
    "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE status = 'shipped')"
  run_analyze "$container" "$tag" "subquery-not-in" \
    "SELECT * FROM users WHERE id NOT IN (SELECT user_id FROM orders WHERE status = 'cancelled')"
  run_analyze "$container" "$tag" "subquery-exists" \
    "SELECT * FROM products p WHERE EXISTS (SELECT 1 FROM reviews r WHERE r.product_id = p.id AND r.rating >= 4)"

  # -- Derived tables --
  run_analyze "$container" "$tag" "derived-table" \
    "SELECT d.user_id, d.order_count FROM (SELECT user_id, COUNT(*) AS order_count FROM orders GROUP BY user_id) d WHERE d.order_count > 5"

  # -- UNION --
  run_analyze "$container" "$tag" "union-all" \
    "SELECT name, 'user' as type FROM users WHERE country = 'US' UNION ALL SELECT name, 'product' FROM products WHERE price > 200"
  run_analyze "$container" "$tag" "union-distinct" \
    "SELECT country FROM users UNION SELECT status FROM orders"

  # -- Window functions (MariaDB 10.2+) --
  run_analyze "$container" "$tag" "window-row-number" \
    "SELECT id, name, ROW_NUMBER() OVER (PARTITION BY country ORDER BY id) rn FROM users"
  run_analyze "$container" "$tag" "window-rank" \
    "SELECT product_id, rating, RANK() OVER (PARTITION BY product_id ORDER BY rating DESC) rnk FROM reviews"

  # -- CTE --
  run_analyze "$container" "$tag" "cte-simple" \
    "WITH top_users AS (SELECT user_id, COUNT(*) cnt FROM orders GROUP BY user_id ORDER BY cnt DESC LIMIT 10) SELECT u.name, t.cnt FROM top_users t JOIN users u ON u.id = t.user_id"

  # -- Complex multi-stage --
  run_analyze "$container" "$tag" "complex-5t-aggregate" \
    "SELECT u.country, COUNT(DISTINCT o.id) AS orders, SUM(oi.quantity * oi.unit_price) AS revenue FROM users u JOIN orders o ON o.user_id = u.id JOIN order_items oi ON oi.order_id = o.id WHERE o.status IN ('shipped','delivered') AND o.created_at >= '2023-06-01' GROUP BY u.country ORDER BY revenue DESC"

  run_analyze "$container" "$tag" "complex-correlated-subquery" \
    "SELECT p.name, p.price, (SELECT AVG(r.rating) FROM reviews r WHERE r.product_id = p.id) avg_rating FROM products p WHERE p.category_id = 1 ORDER BY p.price DESC LIMIT 10"

  log "Generated $FIXTURE_N fixtures for $tag"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

mkdir -p "$OUTPUT_DIR"

# Remove old MariaDB fixtures
rm -f "$OUTPUT_DIR"/mariadb-*.json

# --- MariaDB 11.4 ---
CONTAINER_11="myflames-fixture-mdb114"
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_11}$"; then
  docker rm -f "$CONTAINER_11" > /dev/null
fi

log "Starting MariaDB 11.4 container..."
docker run -d \
  --name "$CONTAINER_11" \
  -e MARIADB_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  -e MARIADB_DATABASE="$MYSQL_DATABASE" \
  mariadb:11.4 \
  > /dev/null

log "Waiting for MariaDB 11.4..."
for i in $(seq 1 60); do
  if docker exec "$CONTAINER_11" \
       mariadb -u root -p"$MYSQL_ROOT_PASSWORD" --silent -e "SELECT 1" 2>/dev/null; then
    log "MariaDB 11.4 is ready (${i}s)"
    break
  fi
  if [ "$i" -eq 60 ]; then
    log "ERROR: MariaDB 11.4 did not become ready"
    docker rm -f "$CONTAINER_11" > /dev/null
    exit 1
  fi
  sleep 1
done

create_schema "$CONTAINER_11"
run_queries "$CONTAINER_11" "11.4"
docker rm -f "$CONTAINER_11" > /dev/null
log "Cleaned up MariaDB 11.4 container"

# --- MariaDB 10.11 ---
CONTAINER_10="myflames-fixture-mdb1011"
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_10}$"; then
  docker rm -f "$CONTAINER_10" > /dev/null
fi

log "Starting MariaDB 10.11 container..."
docker run -d \
  --name "$CONTAINER_10" \
  -e MARIADB_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  -e MARIADB_DATABASE="$MYSQL_DATABASE" \
  mariadb:10.11 \
  > /dev/null

log "Waiting for MariaDB 10.11..."
for i in $(seq 1 60); do
  if docker exec "$CONTAINER_10" \
       mariadb -u root -p"$MYSQL_ROOT_PASSWORD" --silent -e "SELECT 1" 2>/dev/null; then
    log "MariaDB 10.11 is ready (${i}s)"
    break
  fi
  if [ "$i" -eq 60 ]; then
    log "ERROR: MariaDB 10.11 did not become ready"
    docker rm -f "$CONTAINER_10" > /dev/null
    exit 1
  fi
  sleep 1
done

create_schema "$CONTAINER_10"
run_queries "$CONTAINER_10" "10.11"
docker rm -f "$CONTAINER_10" > /dev/null
log "Cleaned up MariaDB 10.11 container"

TOTAL=$(ls -1 "$OUTPUT_DIR"/mariadb-*.json 2>/dev/null | wc -l)
log "Done! Generated $TOTAL MariaDB fixture files in test/fixtures/"
