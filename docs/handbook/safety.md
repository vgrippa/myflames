# Connection policies & write safety

myflames includes features that change server state. Choose the connection's policy to match the environment before using those features.

| Policy | Behavior |
| --- | --- |
| Production | Confirms every change |
| Development | Confirms risky changes such as DDL, KILL, SET GLOBAL, replication skips and CLONE |
| Read-only | Refuses all changes before connecting |

The policy appears in a strip along the window and in write-review sheets. Development is not a read-only setting.

## Review exact SQL

For protected actions, the SQL shown in the review is the SQL authorized for execution. The app binds the review to the statements and target, checks the target again before acting, and records the result in the local Audit log.

Privilege checks can report an action as unknown when they cannot prove that it is allowed. Read the reason rather than assuming unknown means permitted.

## Keep the cost of reads in mind

A read can still consume CPU, I/O and locks. Timed plan analysis runs the SELECT. Read-only policy is a guard against changes, not a resource budget or guarantee of zero production impact.

Use estimated plans first when that is sufficient. Schedule expensive analysis according to your normal operational process.

## Local evidence and credentials

Saved investigations and audit events live on your computer. Saved passwords use a local vault in the desktop backend's `.myflames` directory; server data is rendered locally.

The app can fetch MySQL and MariaDB news headlines while Home is open. That optional setting can be turned off. SSH connections use your own SSH config, keys and agent.

## Share with care

**Hide literal values** replaces SQL and condition literal values with `?` in supported plan exports. It keeps object names, types, counts, measurements and versions. Review the complete artifact before sharing it.

Schema comparison and account-export SQL are generated for review, not automatically executed. Audit records omit passwords; the CSV import audit does not record imported row data.
