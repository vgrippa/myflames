# Why myflames

Performance work is a sequence of questions. Where does this statement spend time? Which operator deserves attention? What changed between two runs? What was the server doing at the same time?

myflames gives those questions one desktop workspace for MySQL, MariaDB and Percona Server for MySQL. **Visual Explain**, **Flame graph**, **Execution tree**, **Bar chart** and **Stats** views make plan hierarchies and operator timings easier to read. Saved investigations and side-by-side comparisons keep context around a change, while **Query editor**, **Query digest**, **Schema versions**, **Server**, **Workload** and the topology places connect a slow statement to live sessions, locks, parameters, I/O and replication.

## A tool for understanding

The workspace helps both a developer reading an unfamiliar plan and a DBA investigating a production incident. Operator details, captured schema and variable evidence, and interactive algorithm lessons in [Learn](learn.md) let you move between a quick visual scan and the engine behavior underneath it.

## Open to inspection

The project is open source under Apache-2.0. You can inspect how every measurement, check or advisor finding is derived, reproduce an issue, build the desktop app from source and contribute fixes.

myflames makes no performance-speedup promise. A useful result is a better-supported decision about the query or server you are investigating.

## The work it builds on

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/).

Brendan Gregg and Tanel Poder are the origin of this visualization inspiration and remain credited across the project. Current flame graph renderers are clean-room implementations. Releases up to 2.3.0 retain their historical CDDL-1.0 license.
