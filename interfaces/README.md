# Interfaces

The contract of QuantumOrb as it stands: signatures, never bodies.

```
interfaces/
  bot/            Sentinel, the Telegram bot: one .pyi per public module
    sources/      the adapters to outside services
    pipeline/     feed, Guard, watch, follow, record, health, the loops
    bot/          the conversation: words, format, keyboards, commands, startup
  observatory/    the site's server: one .d.ts per module, exported names and parameters
    engine/       the live engine, the sample engine, the Orb, the charts, the event bus
    render/       finished HTML for the page
```

Each file opens with the path of the sealed file it was cut from and that file's commitment:

```
# QuantumOrb · interface contract · quantumorb/pipeline/guard.py
# implementation: sealed · commitment 843e7eae6b33eedc…30b542dc
```

so when a file is opened ([`SEAL.md`](../SEAL.md)) its contract and its implementation can be matched
byte for byte. [`INDEX.md`](INDEX.md) lists every contract, the sealed file it came from and how many public
names it declares.

CI holds every contract to the current seal (`python -m vault interfaces`): a contract that names a file
outside the seal, or a version of it other than the sealed one, fails until it is cut again. Python
contracts must parse, and their bodies may hold nothing but a docstring and `...`.

Constants, thresholds, private helpers and anything that names a route stay out. There is nothing here to
import or to run.
