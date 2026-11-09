# Why myflames

Performance work is a sequence of questions. Where does this statement spend time? Which operator deserves attention? What changed between the two runs? What was the server doing at the same time?

myflames gives those questions a shared workspace. Visual plans make a hierarchy easier to inspect. Captures and comparisons keep context around a change. Server views connect the statement to sessions, locks, configuration and I/O.

## A tool for understanding

The workspace should help a developer reading an unfamiliar plan and a DBA following a difficult one. Operator details, supporting evidence and guided lessons let you move between a quick view and a deeper explanation.

## Open to inspection

The project is open source under Apache-2.0. You can inspect how a measurement or finding is derived, reproduce an issue, build the application and contribute a correction.

This website makes no performance-speedup promise. A useful result is a better-supported decision about the query or server you are actually investigating.

## The work it builds on

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/).

Brendan Gregg and Tanel Poder are the origin of this visualization inspiration and remain credited in the project. Current flame graph renderers are clean-room implementations. Releases up to 2.3.0 retain their historical CDDL-1.0 license.
