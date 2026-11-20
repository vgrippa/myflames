"""Live lab suites: unittest modules that run against the Docker cluster labs.

These are not unit tests. They talk to the real servers that
scripts/cluster-matrix.sh brings up (InnoDB Cluster mf-ic-1..3 with a router,
Galera mf-galera-1..3, ProxySQL mf-proxysql), in the spirit of mtr's suites.
`./run-tests.sh` and `python3 -m unittest discover -s test` never pick them
up: the module names do not match the test_*.py discovery pattern, and every
module also skips unless MYFLAMES_LIVE_LABS=1 is set. Run them with
scripts/live-tests.sh (see docs/DEVELOPER_REFERENCE.md, "Live lab suites").
"""
