# Performance

PatchManager is tested with a large inventory on every pull request: 500 racks,
1 000 devices, 50 000 ports and 12 000 cables in one tenant, created through the
regular services (permissions, history and audit included).

```bash
pixi run -e test perf                 # full size, about 30 seconds
PERF_RACKS=50 pixi run -e test perf   # quick local check
```

The test fails when a page takes longer than 1.5 seconds or needs more than 60
database queries, whatever the size of the inventory.

Measured on a laptop (Apple M5, PostgreSQL 18, debug off):

| Page | Time | Queries |
| --- | ---: | ---: |
| Dashboard | 25 ms | 9 |
| Room with 50 racks | 23 ms | 19 |
| Rack drawing with every port | 64 ms | 42 |
| Room drawing with 50 racks | 343 ms | 18 |
| Device with 52 ports, 24 cabled | 142 ms | 28 |
| Cable trace | 10 ms | 28 |
| Cable list | 62 ms | 13 |
| Search | 126 ms | 21 |
| API: ports (paginated) | 26 ms | 10 |
| API: search | 78 ms | 20 |

## Demo data

The same inventory can fill a development instance:

```bash
pixi run -e dev python -m django seed_scale --actor admin@example.test --racks 50
```

Do not run it against production: it creates a tenant with test data.
