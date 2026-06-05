#!/usr/bin/env bash
# docs/examples/run-live-example.sh
#
# Reproduces the token-savings walkthrough end-to-end against a REAL MySQL 8.4
# in Docker: boots a server, seeds a small e-commerce schema, runs a realistic
# analytics query, captures its EXPLAIN ANALYZE FORMAT=JSON, then shows the
# myflames digest + token comparison.
#
# Usage:   ./docs/examples/run-live-example.sh
# Requires: Docker. Leaves docs/examples/orders-revenue-plan.json behind.
set -euo pipefail

NAME="myflames-live-example"
PW="examplepass"
DB="shop"
HERE="$(cd "$(dirname "$0")" && pwd)"
PLAN="$HERE/orders-revenue-plan.json"

# The query a human is staring at, wondering why it's slow:
QUERY="SELECT p.name, SUM(oi.quantity * oi.unit_price) AS revenue
       FROM order_items oi
       JOIN orders o   ON o.id = oi.order_id
       JOIN products p ON p.id = oi.product_id
       WHERE o.status = 'shipped'
       GROUP BY p.id
       ORDER BY revenue DESC
       LIMIT 20"

log() { echo "[live-example] $*"; }

docker rm -f "$NAME" >/dev/null 2>&1 || true
log "Starting MySQL 8.4..."
docker run -d --name "$NAME" -e MYSQL_ROOT_PASSWORD="$PW" -e MYSQL_DATABASE="$DB" mysql:8.4 >/dev/null

log "Waiting for MySQL (TCP probe — the init server bounces, so socket can lie)..."
for i in $(seq 1 90); do
  if docker exec "$NAME" mysql -uroot -p"$PW" -h127.0.0.1 --protocol=TCP --silent -e "SELECT 1" >/dev/null 2>&1; then
    log "ready (${i}s)"; break
  fi
  [ "$i" -eq 90 ] && { log "MySQL never became ready"; docker rm -f "$NAME" >/dev/null; exit 1; }
  sleep 1
done

log "Seeding schema + data..."
docker exec -i "$NAME" mysql -uroot -p"$PW" --silent <<SQL
USE $DB;
SET SESSION cte_max_recursion_depth = 100000;
CREATE TABLE users    (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100), country CHAR(2), INDEX idx_country (country));
CREATE TABLE products (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(200), price DECIMAL(10,2));
CREATE TABLE orders   (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT, status ENUM('pending','processing','shipped','delivered','cancelled'), total DECIMAL(12,2), INDEX idx_user (user_id), INDEX idx_status (status));
CREATE TABLE order_items (id INT AUTO_INCREMENT PRIMARY KEY, order_id INT, product_id INT, quantity INT, unit_price DECIMAL(10,2), INDEX idx_order (order_id), INDEX idx_product (product_id));

INSERT INTO users (name, country)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 3000)
SELECT CONCAT('user', n), ELT(1+(n%5),'US','GB','DE','FR','BR') FROM c;

INSERT INTO products (name, price)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 1500)
SELECT CONCAT('product ', n), ROUND(5 + (n%500) + RAND()*20, 2) FROM c;

INSERT INTO orders (user_id, status, total)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 12000)
SELECT 1+(n%3000), ELT(1+(n%5),'pending','processing','shipped','delivered','cancelled'), ROUND(20+RAND()*480,2) FROM c;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 40000)
SELECT 1+(n%12000), 1+(n%1500), 1+(n%5), ROUND(5+(n%500),2) FROM c;

ANALYZE TABLE users, products, orders, order_items;
SQL

log "Capturing EXPLAIN ANALYZE FORMAT=JSON -> $(basename "$PLAN")"
docker exec -i "$NAME" mysql -uroot -p"$PW" --raw --skip-column-names --silent "$DB" \
  -e "SET explain_json_format_version=2; EXPLAIN ANALYZE FORMAT=JSON $QUERY" > "$PLAN"

echo
echo "================= the human workflow ================="
echo "# 1. You ran the query above and it felt slow. Get its plan:"
echo "#    mysql> EXPLAIN ANALYZE FORMAT=JSON <your query>"
echo "#    (this script saved it to $(basename "$PLAN"))"
echo
echo "# 2a. WITHOUT myflames: paste the whole JSON into your LLM. Its size:"
wc -c "$PLAN" | awk '{print "#     " $1 " bytes of JSON"}'
echo
echo "# 2b. WITH myflames: get the digest and paste THAT instead:"
echo "#     myflames tokens $(basename "$PLAN") --digest | pbcopy"
echo
echo "# 3. See the saving:"
echo "#     myflames tokens $(basename "$PLAN")"
echo "======================================================"
echo
python3 -m myflames tokens "$PLAN"

log "Done. Container '$NAME' left running; remove with: docker rm -f $NAME"
