# sync_django_local

The device half of an offline-first sync protocol for a Django point-of-sale system.
Each POS runs this Django app against its own SQLite database, keeps working with no
network, and syncs with [sync_django_master](https://github.com/Haashir-Shakeel/sync_django_master)
when it can reach it.

In production the same design runs **inside a Flutter app**: Django is embedded with
[serious_python](https://pub.dev/packages/serious_python), the Flutter UI calls it over
localhost, and the sync loop runs in a background thread. `main.py` is that embedded entry point.
The write-up: [Offline-first sync for a Django POS](https://haashir.netlify.app/project.html?p=offline-sync).

## How it works

1. **Every write lands locally first.** Product, Order and Transaction are saved to SQLite.
2. **Signals fill an outbox.** A `post_save` / delete signal writes a `DeviceChange` row with a
   per-device, increasing `local_seq`, the operation and the full serialized payload.
   API views never know about sync; adding an endpoint needs no sync code.
3. **The sync loop pushes.** `python manage.py sync_loop` sends unpushed changes in `local_seq`
   order, in batches (200 by default), with the device's Bearer token.
4. **Results are recorded per change.** Acked changes are marked pushed and `SyncState.last_acked_local_seq`
   moves forward. Conflicts are stored in `LocalConflict` with the server's copy for resolution.
5. **Pulling (implemented, off by default).** `pull_changes` asks the master for `ServerChange`
   rows after `SyncState.last_server_seq` and applies them locally. Rows applied from the master
   are flagged so the signal skips them, which keeps pulled changes from echoing back into the
   outbox. The loop currently runs push-only; the pull call is commented out in `sync_loop.py`.

Retrying is always safe: the master deduplicates on `(device_id, local_seq)`.

## Running on a device

- **Single sync loop.** A `SyncLock` row taken with `select_for_update` keeps two loops from
  running at once; a lock with no heartbeat for 5 minutes is treated as stale and cleared.
- **Schema upgrades on the device.** App updates ship new migrations. `migration_handler.py`
  backs up the SQLite file, migrates, and verifies the schema, adding missing tables or
  columns if a migration was lost. `migration_sync.py` keeps migrations in a persistent
  directory so they survive app updates.
- **Configurable paths.** `DJANGO_DB_PATH` points the database at the app's persistent storage.
  See `.env.example` for every setting.

## Data model

| Model | Purpose |
| --- | --- |
| `Product`, `Order`, `Transaction` | Local business data, same shape as the master. |
| `DeviceChange` | Outbox of local mutations waiting to be pushed. |
| `SyncState` | Last acked `local_seq`, last pulled `server_seq`, last sync time. |
| `LocalConflict` | Conflicts returned by the master, kept for resolution. |
| `LocalDeviceConfig` | Device ID, sync token, business and store scope. |
| `SyncLock` | Guarantees one sync loop per device. |

## Run it

Start the master first (port 8000), then:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py makemigrations master
python manage.py migrate
python manage.py runserver 8001
python manage.py sync_loop --master http://localhost:8000 --device-id POS-1
```

`scripts/create_test_data.py` seeds local data; `scripts/check_sync_status.py` prints outbox and sync state.

## Tests

`scripts/test_sync_integration.py` and `scripts/test_error_tracking.py` check the safe-ack rules: every push
attempt is counted and timestamped, a failed push records its error, and conflicted rows are
never marked as pushed. `scripts/test_ongoing_sync.py` exercises a running loop.
