# Configuration

Copy the examples to the engine root on the server and edit there. The real files are private and are rejected
by CI if committed.

| File | Purpose |
|---|---|
| `site.json` | Website repository and domain (public; copied to the server as is). |
| `live.example.json` → `~/BinanceData/live.json` | Execution engine mode and risk limits. Mode `off` until the owner switches it. |
| `mm.example.json` → `~/BinanceData/mm.json` | Market maker coins and limits. `"enabled": false` until the owner switches it. |
