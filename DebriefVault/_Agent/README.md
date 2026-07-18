# _Agent (working memory)

This folder is the Hermes assistant's working memory: the one and only place it
can write. Everything here is a convenience summary, NOT a clinical record. The
clinical record lives in Clients/ and stays read-only for the agent.

Contents:

- Practice-Snapshot.md: the practice at a glance (roster, risk flags quoted
  verbatim, upcoming follow-ups, recent activity, open homework threads).
- briefs/C-XXXX.md: a short per-client cheat sheet the agent reads first for
  fast context and refreshes after each prep or review task.

Maintained by the maintain-snapshot skill. If a brief ever disagrees with the
client files, the client files win.
