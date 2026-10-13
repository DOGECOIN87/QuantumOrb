# Coding Prompt Template

You are implementing one ticket in the QuantumOrb personal monitoring tool.

Read these files first:

1. `coding-pack/LLM-CODING-RULES.md`
2. `coding-pack/ARCHITECTURE-CONTRACT.md`
3. `coding-pack/IMPLEMENTATION-ORDER.md`
4. The selected ticket under `coding-pack/tickets/`
5. Every schema and golden vector referenced by the ticket

Work only on the selected ticket. Do not implement future tickets. Do not add wallet signing, trading, private-key handling, or public deployment.

Before editing, inspect existing files and preserve established conventions. After editing, run the exact tests named by the ticket, then run the smallest relevant broader test group. If a contract is insufficient, stop and create a decision record instead of guessing.

Return:

```text
Implemented: TICKET-ID
Files changed: ...
Tests run: ...
Tests passed: ...
Known limitations: ...
Contract changes: none | decision-file path
```
