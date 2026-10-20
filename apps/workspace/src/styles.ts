// Every workspace stylesheet, in cascade order: when two rules tie, the later
// file wins, and style.css comes last. main.tsx imports this module first, so
// the order is fixed here and does not depend on which places load their
// chunk first (lazy.tsx). Modules still import their own stylesheet; those
// imports find it already loaded. A new stylesheet is added here too, at the
// position its importer has in this list (test/styles-order.mjs checks).
import "./kit.css";
import "./command-palette.css";
import "./completion.css";
import "./perf.css";
import "./table-data.css";
import "./query-digest.css";
import "./editor.css";
import "./home.css";
import "./digest.css";
import "./health.css";
import "./instruments.css";
import "./locks.css";
import "./accounts.css";
import "./schema-diagram.css";
import "./errorlog.css";
import "./report.css";
import "./tuning.css";
import "./sessions.css";
import "./report-table.css";
import "./tables.css";
import "./config.css";
import "./ops.css";
import "./replication-topology.css";
import "./replication.css";
import "./cluster.css";
import "./server.css";
import "./audit.css";
import "./style.css";
