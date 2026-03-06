#!/usr/bin/env bash
# scripts/generate-fixtures.sh
#
# Generates 50+ MySQL EXPLAIN ANALYZE FORMAT=JSON fixture files using Docker.
# Run once to populate test/fixtures/. Commit the resulting files.
#
# Usage:
#   ./scripts/generate-fixtures.sh
#
# Requirements: Docker

set -euo pipefail

CONTAINER_NAME="myflames-fixture-mysql"
MYSQL_ROOT_PASSWORD="fixturepass"
MYSQL_DATABASE="testdb"
OUTPUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/test/fixtures"
FIXTURE_N=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { echo "[generate-fixtures] $*"; }

mysql_exec() {
  docker exec -i "$CONTAINER_NAME" \
    mysql -u root -p"$MYSQL_ROOT_PASSWORD" --silent
}

# Run an EXPLAIN ANALYZE and save output to test/fixtures/explain-NNN-<desc>.json
run_explain() {
  local desc="$1"
  local sql="$2"
  FIXTURE_N=$((FIXTURE_N + 1))
  local fname
  fname=$(printf "%s/explain-%03d-%s.json" "$OUTPUT_DIR" "$FIXTURE_N" "$desc")
  docker exec -i "$CONTAINER_NAME" \
    mysql -u root -p"$MYSQL_ROOT_PASSWORD" \
    --raw --skip-column-names --silent \
    "$MYSQL_DATABASE" 2>/dev/null \
    -e "SET explain_json_format_version=2; EXPLAIN ANALYZE FORMAT=JSON $sql" \
    > "$fname"
  log "  [$FIXTURE_N] $desc -> $(basename "$fname")"
}

# ---------------------------------------------------------------------------
# Start MySQL 8.4 container
# ---------------------------------------------------------------------------

mkdir -p "$OUTPUT_DIR"

if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  log "Removing existing container $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME" > /dev/null
fi

log "Starting MySQL 8.4 container..."
docker run -d \
  --platform linux/arm64 \
  --name "$CONTAINER_NAME" \
  -e MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  -e MYSQL_DATABASE="$MYSQL_DATABASE" \
  mysql:8.4 \
  > /dev/null

log "Waiting for MySQL to be ready..."
for i in $(seq 1 90); do
  if docker exec "$CONTAINER_NAME" \
       mysql -u root -p"$MYSQL_ROOT_PASSWORD" --silent -e "SELECT 1" 2>/dev/null; then
    log "MySQL is ready (${i}s)"
    break
  fi
  if [ "$i" -eq 90 ]; then
    log "ERROR: MySQL did not become ready in 90s"
    docker rm -f "$CONTAINER_NAME" > /dev/null
    exit 1
  fi
  sleep 1
done

# ---------------------------------------------------------------------------
# Schema + data
# ---------------------------------------------------------------------------

log "Creating schema and seeding data..."
mysql_exec << 'SQL'
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
  status     ENUM('pending','processing','shipped','delivered','cancelled') NOT NULL,
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

-- Seed: categories (50 rows via recursive CTE)
SET cte_max_recursion_depth = 100000;
INSERT INTO categories (name, parent_id)
WITH RECURSIVE cte(n) AS (
  SELECT 1 UNION ALL SELECT n+1 FROM cte WHERE n < 50
)
SELECT CONCAT('Category-', n),
       IF(n > 10, 1 + FLOOR((n-11)/5), NULL)
FROM cte;

-- Seed: users (3000 rows)
INSERT INTO users (name, email, country, created_at)
WITH RECURSIVE cte(n) AS (
  SELECT 1 UNION ALL SELECT n+1 FROM cte WHERE n < 3000
)
SELECT CONCAT('User ', n),
       CONCAT('user', n, '@example.com'),
       ELT(1 + MOD(n, 5), 'US','UK','DE','FR','JP'),
       DATE_ADD('2020-01-01', INTERVAL MOD(n * 7, 1460) DAY)
FROM cte;

-- Seed: products (1500 rows)
INSERT INTO products (name, category_id, price, stock, created_at)
WITH RECURSIVE cte(n) AS (
  SELECT 1 UNION ALL SELECT n+1 FROM cte WHERE n < 1500
)
SELECT CONCAT('Product-', n),
       1 + MOD(n, 50),
       ROUND(5 + MOD(n * 13, 995) + 0.99, 2),
       MOD(n * 3, 500),
       DATE_ADD('2021-01-01', INTERVAL MOD(n * 11, 730) DAY)
FROM cte;

-- Seed: orders (12000 rows)
INSERT INTO orders (user_id, status, total, created_at)
WITH RECURSIVE cte(n) AS (
  SELECT 1 UNION ALL SELECT n+1 FROM cte WHERE n < 12000
)
SELECT 1 + MOD(n * 7, 3000),
       ELT(1 + MOD(n, 5), 'pending','processing','shipped','delivered','cancelled'),
       ROUND(10 + MOD(n * 17, 990) + 0.99, 2),
       DATE_ADD('2022-01-01', INTERVAL MOD(n * 3, 730) DAY)
FROM cte;

-- Seed: order_items (40000 rows)
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
WITH RECURSIVE cte(n) AS (
  SELECT 1 UNION ALL SELECT n+1 FROM cte WHERE n < 40000
)
SELECT 1 + MOD(n * 3, 12000),
       1 + MOD(n * 11, 1500),
       1 + MOD(n, 10),
       ROUND(5 + MOD(n * 13, 495) + 0.99, 2)
FROM cte;

-- Seed: reviews (10000 rows)
INSERT INTO reviews (product_id, user_id, rating, created_at)
WITH RECURSIVE cte(n) AS (
  SELECT 1 UNION ALL SELECT n+1 FROM cte WHERE n < 10000
)
SELECT 1 + MOD(n * 7, 1500),
       1 + MOD(n * 13, 3000),
       1 + MOD(n, 5),
       DATE_ADD('2022-06-01', INTERVAL MOD(n * 5, 600) DAY)
FROM cte;

ANALYZE TABLE categories, users, products, orders, order_items, reviews;
SQL

log "Schema and data ready."

# ---------------------------------------------------------------------------
# Generate EXPLAIN fixtures
# ---------------------------------------------------------------------------

log "Generating EXPLAIN ANALYZE fixtures..."

# --- Simple table scans (no useful index path) ---
run_explain "table-scan-users-no-filter" \
  "SELECT * FROM users"

run_explain "table-scan-categories" \
  "SELECT * FROM categories"

run_explain "table-scan-products-all" \
  "SELECT * FROM products"

# --- Single-row primary key lookups ---
run_explain "pk-lookup-user" \
  "SELECT * FROM users WHERE id = 42"

run_explain "pk-lookup-product" \
  "SELECT * FROM products WHERE id = 100"

run_explain "pk-lookup-order" \
  "SELECT * FROM orders WHERE id = 500"

run_explain "pk-lookup-review" \
  "SELECT * FROM reviews WHERE id = 1"

# --- Index scans ---
run_explain "index-scan-users-by-country" \
  "SELECT id, email FROM users WHERE country = 'US'"

run_explain "index-scan-orders-by-status" \
  "SELECT id, total FROM orders WHERE status = 'shipped'"

run_explain "index-scan-products-by-category" \
  "SELECT id, name, price FROM products WHERE category_id = 3"

# --- Index range scans ---
run_explain "index-range-scan-users-created" \
  "SELECT * FROM users WHERE created_at BETWEEN '2021-01-01' AND '2021-12-31'"

run_explain "index-range-scan-products-price" \
  "SELECT * FROM products WHERE price BETWEEN 50.00 AND 200.00"

run_explain "index-range-scan-orders-created" \
  "SELECT * FROM orders WHERE created_at >= '2023-01-01'"

run_explain "index-range-scan-products-price-high" \
  "SELECT * FROM products WHERE price > 800.00"

run_explain "index-range-scan-reviews-rating" \
  "SELECT * FROM reviews WHERE rating >= 4"

# --- Covering index scans ---
run_explain "covering-index-cat-price" \
  "SELECT category_id, price FROM products WHERE category_id = 5 ORDER BY price"

run_explain "covering-index-prod-rating" \
  "SELECT product_id, rating FROM reviews WHERE product_id = 10"

run_explain "covering-index-user-status" \
  "SELECT user_id, status FROM orders WHERE user_id = 100"

# --- Filters ---
run_explain "filter-users-country-and-date" \
  "SELECT * FROM users WHERE country = 'DE' AND created_at > '2022-01-01'"

run_explain "filter-products-price-and-stock" \
  "SELECT * FROM products WHERE price < 100.00 AND stock > 50"

run_explain "filter-orders-status-and-total" \
  "SELECT * FROM orders WHERE status = 'delivered' AND total > 500.00"

run_explain "filter-with-like" \
  "SELECT * FROM users WHERE name LIKE 'User 1%'"

# --- Sorts (filesort) ---
run_explain "sort-users-by-name" \
  "SELECT * FROM users ORDER BY name"

run_explain "sort-products-by-price-desc" \
  "SELECT * FROM products ORDER BY price DESC"

run_explain "sort-orders-by-total-desc" \
  "SELECT * FROM orders ORDER BY total DESC"

# --- Sort with LIMIT (top-N) ---
run_explain "sort-limit-top10-expensive-products" \
  "SELECT * FROM products ORDER BY price DESC LIMIT 10"

run_explain "sort-limit-top5-largest-orders" \
  "SELECT * FROM orders ORDER BY total DESC LIMIT 5"

run_explain "sort-limit-with-offset" \
  "SELECT * FROM products ORDER BY price LIMIT 20 OFFSET 100"

# --- Aggregates ---
run_explain "aggregate-count-users" \
  "SELECT COUNT(*) FROM users"

run_explain "aggregate-count-by-country" \
  "SELECT country, COUNT(*) AS cnt FROM users GROUP BY country"

run_explain "aggregate-sum-orders-by-user" \
  "SELECT user_id, SUM(total) AS total_spent FROM orders GROUP BY user_id ORDER BY total_spent DESC LIMIT 20"

run_explain "aggregate-avg-rating-per-product" \
  "SELECT product_id, AVG(rating) AS avg_rating, COUNT(*) AS review_count FROM reviews GROUP BY product_id HAVING COUNT(*) >= 5"

run_explain "aggregate-max-price-by-category" \
  "SELECT category_id, MAX(price) AS max_price, MIN(price) AS min_price FROM products GROUP BY category_id"

run_explain "aggregate-count-distinct-users-ordered" \
  "SELECT COUNT(DISTINCT user_id) FROM orders WHERE status = 'delivered'"

# --- 2-table INNER JOINs ---
run_explain "join-2t-users-orders" \
  "SELECT u.name, o.total FROM users u JOIN orders o ON o.user_id = u.id WHERE u.country = 'US' LIMIT 100"

run_explain "join-2t-orders-items" \
  "SELECT o.id, oi.product_id, oi.quantity FROM orders o JOIN order_items oi ON oi.order_id = o.id WHERE o.status = 'shipped' LIMIT 100"

run_explain "join-2t-products-category" \
  "SELECT p.name, c.name AS cat FROM products p JOIN categories c ON c.id = p.category_id WHERE p.price > 500"

run_explain "join-2t-products-reviews" \
  "SELECT p.name, r.rating FROM products p JOIN reviews r ON r.product_id = p.id WHERE r.rating = 5 LIMIT 50"

# --- LEFT JOINs ---
run_explain "left-join-users-orders" \
  "SELECT u.id, u.name, COUNT(o.id) AS order_count FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.id, u.name LIMIT 100"

run_explain "left-join-products-reviews" \
  "SELECT p.id, p.name, r.rating FROM products p LEFT JOIN reviews r ON r.product_id = p.id WHERE p.category_id = 1"

run_explain "left-join-categories-parent" \
  "SELECT c.name, p.name AS parent FROM categories c LEFT JOIN categories p ON p.id = c.parent_id"

# --- 3-table JOINs ---
run_explain "join-3t-users-orders-items" \
  "SELECT u.name, o.id, oi.product_id FROM users u JOIN orders o ON o.user_id = u.id JOIN order_items oi ON oi.order_id = o.id WHERE u.country = 'JP' LIMIT 50"

run_explain "join-3t-orders-items-products" \
  "SELECT o.id, p.name, oi.quantity FROM orders o JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id WHERE o.status = 'delivered' LIMIT 50"

run_explain "join-3t-products-category-reviews" \
  "SELECT p.name, c.name AS cat, AVG(r.rating) AS avg_r FROM products p JOIN categories c ON c.id = p.category_id JOIN reviews r ON r.product_id = p.id GROUP BY p.id, p.name, c.name LIMIT 20"

# --- 4-table JOINs ---
run_explain "join-4t-users-orders-items-products" \
  "SELECT u.name, p.name, oi.quantity FROM users u JOIN orders o ON o.user_id = u.id JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id WHERE u.country = 'UK' LIMIT 30"

# --- 5-table JOIN ---
run_explain "join-5t-full-chain" \
  "SELECT u.name, c.name AS cat, p.name AS prod, oi.quantity, o.total FROM users u JOIN orders o ON o.user_id = u.id JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id JOIN categories c ON c.id = p.category_id WHERE o.status = 'delivered' AND u.country = 'US' LIMIT 20"

# --- Subqueries (semi-join / IN / EXISTS) ---
run_explain "semi-join-users-with-orders" \
  "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE status = 'delivered')"

run_explain "semi-join-products-reviewed" \
  "SELECT * FROM products WHERE id IN (SELECT product_id FROM reviews WHERE rating = 5)"

run_explain "semi-join-exists-orders" \
  "SELECT * FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.total > 500)"

# --- Anti-joins (NOT IN / NOT EXISTS) ---
run_explain "anti-join-users-no-orders" \
  "SELECT * FROM users WHERE id NOT IN (SELECT user_id FROM orders)"

run_explain "anti-join-products-no-reviews" \
  "SELECT * FROM products WHERE NOT EXISTS (SELECT 1 FROM reviews WHERE reviews.product_id = products.id)"

# --- Derived tables / subquery in FROM ---
run_explain "derived-table-top-spenders" \
  "SELECT u.name, agg.total_spent FROM users u JOIN (SELECT user_id, SUM(total) AS total_spent FROM orders GROUP BY user_id) agg ON agg.user_id = u.id ORDER BY agg.total_spent DESC LIMIT 10"

run_explain "derived-table-product-avg-rating" \
  "SELECT p.name, sub.avg_rating FROM products p JOIN (SELECT product_id, AVG(rating) AS avg_rating FROM reviews GROUP BY product_id) sub ON sub.product_id = p.id WHERE sub.avg_rating >= 4 LIMIT 20"

# --- CTEs ---
run_explain "cte-top-users-by-spend" \
  "WITH top_users AS (SELECT user_id, SUM(total) AS total_spent FROM orders GROUP BY user_id ORDER BY total_spent DESC LIMIT 100) SELECT u.name, t.total_spent FROM users u JOIN top_users t ON t.user_id = u.id"

run_explain "cte-multi-products-with-stats" \
  "WITH prod_stats AS (SELECT product_id, COUNT(*) AS cnt, AVG(rating) AS avg_r FROM reviews GROUP BY product_id), top_products AS (SELECT product_id FROM prod_stats WHERE avg_r >= 4 AND cnt >= 3) SELECT p.name, ps.avg_r FROM products p JOIN prod_stats ps ON ps.product_id = p.id WHERE p.id IN (SELECT product_id FROM top_products)"

run_explain "cte-category-hierarchy" \
  "WITH RECURSIVE cat_tree AS (SELECT id, name, parent_id, 0 AS depth FROM categories WHERE parent_id IS NULL UNION ALL SELECT c.id, c.name, c.parent_id, ct.depth+1 FROM categories c JOIN cat_tree ct ON ct.id = c.parent_id) SELECT * FROM cat_tree"

# --- Window functions ---
run_explain "window-row-number-by-country" \
  "SELECT id, name, country, ROW_NUMBER() OVER (PARTITION BY country ORDER BY created_at) AS rn FROM users LIMIT 100"

run_explain "window-rank-products-by-price" \
  "SELECT id, name, category_id, price, RANK() OVER (PARTITION BY category_id ORDER BY price DESC) AS price_rank FROM products"

run_explain "window-running-total-orders" \
  "SELECT id, user_id, total, SUM(total) OVER (PARTITION BY user_id ORDER BY created_at) AS running_total FROM orders WHERE user_id <= 100"

run_explain "window-lag-lead-orders" \
  "SELECT id, user_id, total, LAG(total) OVER (PARTITION BY user_id ORDER BY created_at) AS prev_total FROM orders WHERE user_id BETWEEN 1 AND 50"

# --- UNION ---
run_explain "union-all-us-uk-users" \
  "(SELECT id, name, 'US' AS src FROM users WHERE country = 'US' LIMIT 10) UNION ALL (SELECT id, name, 'UK' FROM users WHERE country = 'UK' LIMIT 10)"

run_explain "union-distinct-shipped-delivered" \
  "SELECT user_id FROM orders WHERE status = 'shipped' UNION SELECT user_id FROM orders WHERE status = 'delivered'"

# --- LIMIT without ORDER ---
run_explain "limit-no-order-users" \
  "SELECT * FROM users LIMIT 50"

run_explain "limit-with-order-products" \
  "SELECT * FROM products ORDER BY created_at DESC LIMIT 25"

# --- Complex combinations ---
run_explain "complex-join-agg-sort" \
  "SELECT u.country, COUNT(DISTINCT o.id) AS orders, SUM(oi.quantity * oi.unit_price) AS revenue FROM users u JOIN orders o ON o.user_id = u.id JOIN order_items oi ON oi.order_id = o.id WHERE o.status = 'delivered' GROUP BY u.country ORDER BY revenue DESC"

run_explain "complex-cte-window-filter" \
  "WITH ranked AS (SELECT product_id, rating, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY created_at DESC) AS rn FROM reviews) SELECT p.name, r.rating FROM products p JOIN ranked r ON r.product_id = p.id WHERE r.rn = 1 AND p.price > 100 LIMIT 30"

run_explain "complex-derived-join-group" \
  "SELECT cat.name, top.cnt, top.avg_price FROM categories cat JOIN (SELECT category_id, COUNT(*) AS cnt, AVG(price) AS avg_price FROM products WHERE stock > 0 GROUP BY category_id HAVING cnt >= 5) top ON top.category_id = cat.id ORDER BY top.avg_price DESC"

run_explain "complex-5t-aggregate" \
  "SELECT c.name AS category, u.country, COUNT(DISTINCT o.id) AS order_count, SUM(oi.quantity) AS units FROM categories c JOIN products p ON p.category_id = c.id JOIN order_items oi ON oi.product_id = p.id JOIN orders o ON o.id = oi.order_id JOIN users u ON u.id = o.user_id WHERE o.status IN ('shipped','delivered') GROUP BY c.name, u.country HAVING order_count >= 10 ORDER BY units DESC LIMIT 20"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

log "Stopping and removing container..."
docker rm -f "$CONTAINER_NAME" > /dev/null

echo ""
log "Done! Generated $FIXTURE_N fixture files in $OUTPUT_DIR/"
log "Run 'git add test/fixtures/' and commit the results."
