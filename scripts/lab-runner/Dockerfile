# Runner for the live MySQL Shell suites: MySQL Shell and the mysql client on
# PATH (both ship in the mysql:8.4 image) plus Python for the scenario.
# scripts/live-tests.sh and test/live/live_shell.py build it as
# myflames-lab-runner:8.4 when it is missing.
FROM mysql:8.4
RUN microdnf install -y python3 && microdnf clean all
