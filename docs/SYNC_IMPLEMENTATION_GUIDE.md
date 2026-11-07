# POS Offline Sync — Full Implementation Guide

> **Based on:** `sync_django_local` (local/POS device) + `sync_django_master` (master server)  
> **Proven working:** Flutter → Local Django (SQLite) → Sync Loop → Master Django (PostgreSQL) on `localhost:8000`

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Core Concepts](#2-core-concepts)
3. [Local (POS Device) — Full Implementation](#3-local-pos-device--full-implementation)
4. [Master (Server) — Full Implementation](#4-master-server--full-implementation)
5. [Communication Layer](#5-communication-layer)
6. [Foreign Keys + UUID Pattern — The Complete Rule](#6-foreign-keys--uuid-pattern--the-complete-rule)
7. [Full Example: Transaction Model with 5 Foreign Keys](#7-full-example-transaction-model-with-5-foreign-keys)
8. [Multi-Model API (4–5 models in 1 API call)](#8-multi-model-api-45-models-in-1-api-call)
9. [Scaling to 100–200 APIs](#9-scaling-to-100200-apis)
10. [Conflict Resolution Rules](#10-conflict-resolution-rules)
11. [Adding a New Sync Model — Checklist](#11-adding-a-new-sync-model--checklist)
12. [Running the System](#12-running-the-system)

---

## 1. System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Android POS Device                                              │
│                                                                  │
│   Flutter UI                                                     │
│       │  (Serial Python / HTTP to localhost)                     │
│       ▼                                                          │
│   Local Django (SQLite)          ← your business APIs live here │
│       │                                                          │
│       │  post_save signals                                       │
│       ▼                                                          │
│   DeviceChange Outbox Table      ← append-only log of changes   │
│       │                                                          │
│       │  sync_loop management command (every 3 seconds)         │
│       ▼                                                          │
│   HTTP POST /master/sync/push/   ← single generic endpoint      │
└──────────────────────────────────────────────────────────────────┘
                          │
                          │  JSON batch of changes
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  Master Server (PostgreSQL)                                      │
│                                                                  │
│   SyncPushView                                                   │
│       │  validates schema_version, device identity              │
│       │  applies changes in local_seq order                     │
│       │  resolves UUID references → FK IDs                      │
│       │  records ProcessedDeviceChange (idempotency)            │
│       │  records ServerChange log                               │
│       ▼                                                          │
│   Response: acked_up_to_local_seq + results[] + conflicts[]     │
└──────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**
- **Push-only** (local → master). Pull is implemented but not active in MVP.
- **One generic sync endpoint** for all models — not one endpoint per API.
- **UUID is the sync identity.** Master's integer `id` is its own internal concern.
- **Outbox pattern** — signals write to `DeviceChange`, sync loop reads and ships.
- **Idempotent** — master records every processed `(device_id, local_seq)` pair. Retries are safe.

---

## 2. Core Concepts

### 2.1 UUIDs vs Integer IDs

| Where | Primary Key | Purpose |
|---|---|---|
| **Local (SQLite)** | `uuid` (UUID4) | Only identity local needs. UUID is PK. |
| **Master (PostgreSQL)** | `id` (BigAutoField) | Fast joins, internal master DB use |
| **Master (PostgreSQL)** | `uuid` (UUIDField, unique) | Sync identity, cross-device identity |

> **Rule:** Never send integer IDs over the wire. All sync payloads use UUIDs only.

### 2.2 Foreign Key Rule

Every FK on a sync-enabled model must have a companion `*_uuid` field:

```python
# CORRECT — both fields exist
related_order_uuid = models.UUIDField(null=True, blank=True)   # for sync
related_order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)  # for ORM

# WRONG — FK only, no UUID
related_order = models.ForeignKey(Order, ...)  # cannot sync this
```

The `*_uuid` field is what travels in the sync payload. The FK is resolved on master after arrival.

### 2.3 The Outbox (DeviceChange)

Every time a tracked model is saved, a signal creates a `DeviceChange` row:

```
DeviceChange row:
  local_seq  = auto-increment PK (ordering guarantee)
  device_id  = 'POS-1'
  model      = 'transaction'
  object_uuid = <uuid of the saved object>
  op         = 'upsert' or 'delete'
  payload    = JSON snapshot of the object (UUID fields only, no FK IDs)
  pushed     = False
```

The sync loop reads rows where `pushed=False`, ships them in `local_seq` order, and marks them `pushed=True` after master acknowledges.

### 2.4 Version Field

Every sync model has `version = PositiveIntegerField(default=1)`.

- Local auto-increments on every update (in `SyncBaseModel.save()`).
- Master uses version for conflict detection on Products (server rejects if `incoming_version < existing_obj.version`).
- Always send `version` in the payload.

---

## 3. Local (POS Device) — Full Implementation

### 3.1 SyncBaseModel

All sync-enabled models inherit from this. Put it in your local `models.py`:

```python
import uuid
from django.db import models
from django.utils import timezone


class SyncBaseModel(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not getattr(self, '_syncing_from_master', False):
            if self.pk is not None:
                try:
                    current = self.__class__.objects.get(pk=self.pk)
                    if current.version == self.version:
                        self.version += 1
                except self.__class__.DoesNotExist:
                    pass
        super().save(*args, **kwargs)

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.version += 1
        self.save()

    def is_deleted(self):
        return self.deleted_at is not None
```

> **Critical:** Never call `.delete()` on sync models. Always call `.soft_delete()`. Hard deletes cannot be synced.

### 3.2 Sync Infrastructure Models

Add these to your local `models.py`. These are not business models — they are sync plumbing:

```python
class DeviceChange(models.Model):
    OPERATION_CHOICES = [('upsert', 'Upsert'), ('delete', 'Delete')]

    device_id = models.CharField(max_length=50)
    local_seq = models.AutoField(primary_key=True)
    model = models.CharField(max_length=50)
    object_uuid = models.UUIDField()
    op = models.CharField(max_length=10, choices=OPERATION_CHOICES)
    payload = models.JSONField()
    ts = models.DateTimeField(auto_now_add=True)
    pushed = models.BooleanField(default=False)
    pushed_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ['local_seq']


class SyncState(models.Model):
    device_id = models.CharField(max_length=50, primary_key=True)
    last_acked_local_seq = models.PositiveIntegerField(default=0)
    last_server_seq = models.PositiveIntegerField(default=0)
    last_sync_ts = models.DateTimeField(null=True, blank=True)


class LocalConflict(models.Model):
    device_id = models.CharField(max_length=50)
    local_seq = models.PositiveIntegerField()
    model = models.CharField(max_length=50)
    object_uuid = models.UUIDField()
    conflict_data = models.JSONField()
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['device_id', 'local_seq']


class LocalDeviceConfig(models.Model):
    device_id = models.CharField(max_length=50, unique=True, primary_key=True)
    sync_token = models.CharField(max_length=255)
    master_url = models.URLField()
    business_uuid = models.UUIDField()
    store_uuid = models.UUIDField(null=True, blank=True)
    sync_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class SyncLock(models.Model):
    lock_name = models.CharField(max_length=50, primary_key=True)
    device_id = models.CharField(max_length=50)
    process_id = models.IntegerField()
    acquired_at = models.DateTimeField(auto_now_add=True)
    last_heartbeat = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
```

### 3.3 Signals — The Outbox Writer

Create `signals.py` in your local app. This file is the bridge between your business logic and the sync system. **You never touch it when adding APIs** — you only add new senders when adding new sync models:

```python
import json
from decimal import Decimal
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from .models import (
    DeviceChange, SyncState,
    # --- Register every sync model here ---
    DrawerSession, Order, OrderItem, Transaction, Payment, ReasonCode,
    # add more models as you add them
)


def get_model_name(instance):
    """Maps model class → string name used in DeviceChange.model"""
    model_map = {
        DrawerSession: 'drawer_session',
        Order: 'order',
        OrderItem: 'order_item',
        Transaction: 'transaction',
        Payment: 'payment',
        ReasonCode: 'reason_code',
        # add more here
    }
    return model_map.get(type(instance))


def serialize_instance(instance):
    """
    Serialize to JSON payload.
    Rules:
      - Always include uuid
      - Include all *_uuid companion fields (FK references)
      - Never include FK id fields (drawer_session_id, related_order_id, etc.)
      - Serialize Decimal as string
      - Serialize datetime as ISO string
      - Serialize UUID as string
    """
    data = {}
    data['uuid'] = str(instance.uuid)

    for field in instance._meta.fields:
        field_name = field.name
        if field_name == 'uuid':
            continue

        # Skip FK id columns — never sync integer IDs
        if field_name.endswith('_id') and field_name != 'store_id':
            continue

        field_value = getattr(instance, field_name)

        if field_name in ['created_at', 'updated_at', 'deleted_at']:
            data[field_name] = field_value.isoformat() if field_value else None
        elif isinstance(field_value, Decimal):
            data[field_name] = str(field_value)
        elif field_value is not None and hasattr(field_value, 'hex'):  # UUID
            data[field_name] = str(field_value)
        else:
            data[field_name] = field_value

    return data


def create_device_change(instance, operation):
    device_id = getattr(settings, 'DEVICE_ID', 'POS-1')
    model_name = get_model_name(instance)
    if not model_name:
        return

    SyncState.objects.get_or_create(
        device_id=device_id,
        defaults={'last_acked_local_seq': 0, 'last_server_seq': 0, 'last_sync_ts': None}
    )

    DeviceChange.objects.create(
        device_id=device_id,
        model=model_name,
        object_uuid=instance.uuid,
        op=operation,
        payload=serialize_instance(instance),
        pushed=False
    )


# --- Register signals for every sync model ---
@receiver(post_save, sender=DrawerSession)
@receiver(post_save, sender=Order)
@receiver(post_save, sender=OrderItem)
@receiver(post_save, sender=Transaction)
@receiver(post_save, sender=Payment)
@receiver(post_save, sender=ReasonCode)
def handle_model_save(sender, instance, created, **kwargs):
    if getattr(instance, '_syncing_from_master', False):
        return  # Never re-sync changes that came FROM master

    if instance.deleted_at is not None:
        create_device_change(instance, 'delete')
    else:
        create_device_change(instance, 'upsert')
```

### 3.4 Register Signals in apps.py

```python
from django.apps import AppConfig


class YourAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'your_app'

    def ready(self):
        import your_app.signals  # Replace with your actual app name
```

### 3.5 Sync Loop — Management Command

This runs as a background process on the POS device. File: `management/commands/sync_loop.py`

```python
import time
import os
import requests
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from django.db import transaction, IntegrityError
from your_app.models import DeviceChange, SyncState, LocalConflict, SyncLock, LocalDeviceConfig


class Command(BaseCommand):
    help = 'Sync loop: push local changes to master every N seconds'

    def add_arguments(self, parser):
        parser.add_argument('--master', type=str,
                            default=getattr(settings, 'MASTER_URL', 'http://localhost:8000'))
        parser.add_argument('--device-id', type=str,
                            default=getattr(settings, 'DEVICE_ID', 'POS-1'))
        parser.add_argument('--batch-size', type=int, default=200)
        parser.add_argument('--sleep-interval', type=int, default=3)

    def handle(self, *args, **options):
        master_url = options['master'].rstrip('/')
        device_id = options['device_id']
        batch_size = options['batch_size']
        sleep_interval = options['sleep_interval']

        try:
            cfg = LocalDeviceConfig.objects.get(device_id=device_id)
            if not cfg.sync_enabled:
                self.stdout.write(self.style.WARNING('Sync disabled in config'))
                return
            if cfg.master_url:
                master_url = cfg.master_url.rstrip('/')
            sync_token = cfg.sync_token
            business_uuid = cfg.business_uuid
            store_uuid = cfg.store_uuid
        except LocalDeviceConfig.DoesNotExist:
            sync_token = None
            business_uuid = None
            store_uuid = None

        if not self.acquire_lock(device_id):
            return

        sync_state, _ = SyncState.objects.get_or_create(
            device_id=device_id,
            defaults={'last_acked_local_seq': 0, 'last_server_seq': 0}
        )

        try:
            while True:
                try:
                    self.push_changes(master_url, device_id, batch_size,
                                      sync_state, sync_token, business_uuid, store_uuid)
                    sync_state.last_sync_ts = timezone.now()
                    sync_state.save()
                    time.sleep(sleep_interval)
                except requests.RequestException as e:
                    self.stdout.write(self.style.ERROR(f'Network error: {e}'))
                    time.sleep(sleep_interval)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Error: {e}'))
                    time.sleep(sleep_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.release_lock(device_id)

    def push_changes(self, master_url, device_id, batch_size,
                     sync_state, sync_token, business_uuid, store_uuid):
        unpushed = list(
            DeviceChange.objects.filter(
                device_id=device_id,
                local_seq__gt=sync_state.last_acked_local_seq,
                pushed=False
            ).order_by('local_seq')[:batch_size]
        )

        if not unpushed:
            return

        changes_data = [
            {
                'local_seq': c.local_seq,
                'model': c.model,
                'object_uuid': str(c.object_uuid),
                'op': c.op,
                'payload': c.payload,
                'ts': c.ts.isoformat()
            }
            for c in unpushed
        ]

        payload = {
            'device_id': device_id,
            'last_acked_local_seq': sync_state.last_acked_local_seq,
            'schema_version': 1,
            'changes': changes_data
        }
        if business_uuid:
            payload['business_uuid'] = str(business_uuid)
        if store_uuid:
            payload['store_uuid'] = str(store_uuid)

        headers = {'Content-Type': 'application/json'}
        if sync_token:
            headers['Authorization'] = f'Bearer {sync_token}'

        for c in unpushed:
            c.attempt_count += 1
            c.last_attempt_at = timezone.now()
            c.save(update_fields=['attempt_count', 'last_attempt_at'])

        response = requests.post(
            f'{master_url}/master/sync/push/',
            json=payload,
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        result = response.json()

        conflicted_seqs = set()
        if result.get('conflicts'):
            conflicted_seqs = self.handle_conflicts(device_id, result['conflicts'])

        if 'acked_up_to_local_seq' in result:
            acked_seq = result['acked_up_to_local_seq']
            sync_state.last_acked_local_seq = max(sync_state.last_acked_local_seq, acked_seq)
            sync_state.save()

            q = DeviceChange.objects.filter(
                device_id=device_id,
                local_seq__lte=acked_seq,
                pushed=False
            )
            if conflicted_seqs:
                q = q.exclude(local_seq__in=conflicted_seqs)
            q.update(pushed=True, pushed_at=timezone.now(), last_error=None)

            self.stdout.write(f'Acked up to seq {acked_seq}')

    def handle_conflicts(self, device_id, conflicts):
        conflicted_seqs = set()
        for conflict in conflicts:
            local_seq = conflict['local_seq']
            conflicted_seqs.add(local_seq)
            LocalConflict.objects.update_or_create(
                device_id=device_id,
                local_seq=local_seq,
                defaults={
                    'model': conflict.get('model', 'unknown'),
                    'object_uuid': conflict['object_uuid'],
                    'conflict_data': conflict,
                    'resolved': False
                }
            )
        return conflicted_seqs

    def acquire_lock(self, device_id):
        lock_name = f'sync_loop_{device_id}'
        process_id = os.getpid()
        stale_threshold = timezone.now() - timezone.timedelta(minutes=5)
        SyncLock.objects.filter(lock_name=lock_name, last_heartbeat__lt=stale_threshold).delete()
        try:
            with transaction.atomic():
                existing = SyncLock.objects.select_for_update().filter(lock_name=lock_name).first()
                if existing:
                    if existing.last_heartbeat > stale_threshold:
                        self.stdout.write(self.style.ERROR('Sync loop already running'))
                        return False
                    existing.delete()
                SyncLock.objects.create(lock_name=lock_name, device_id=device_id,
                                        process_id=process_id, is_active=True)
            return True
        except IntegrityError:
            return False

    def release_lock(self, device_id):
        SyncLock.objects.filter(
            lock_name=f'sync_loop_{device_id}',
            process_id=os.getpid()
        ).delete()
```

### 3.6 Local Settings

```python
# settings.py (local)
DEVICE_ID = 'POS-1'
MASTER_URL = 'http://localhost:8000'  # or production URL
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}
```

---

## 4. Master (Server) — Full Implementation

### 4.1 Master SyncBaseModel

On master, models have **both** `id` (internal integer PK) **and** `uuid` (sync identity):

```python
import uuid
from django.db import models
from django.utils import timezone


class SyncBaseModel(models.Model):
    id = models.BigAutoField(primary_key=True)          # Master internal PK — fast joins
    uuid = models.UUIDField(unique=True, db_index=True,
                            default=uuid.uuid4)          # Sync identity — cross-device
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True

    def soft_delete(self, syncing_from_device=False):
        if syncing_from_device:
            self._syncing_from_device = True
        self.deleted_at = timezone.now()
        self.version += 1
        self.save()

    def is_deleted(self):
        return self.deleted_at is not None
```

### 4.2 Master Sync Infrastructure Models

```python
class ServerChange(models.Model):
    """Append-only log of all server-side changes. Used for pull sync."""
    OPERATION_CHOICES = [('upsert', 'Upsert'), ('delete', 'Delete')]

    seq = models.AutoField(primary_key=True)
    model = models.CharField(max_length=50)
    device_id = models.CharField(max_length=50, null=True, blank=True)
    object_uuid = models.UUIDField()
    op = models.CharField(max_length=10, choices=OPERATION_CHOICES)
    payload = models.JSONField()
    server_ts = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['seq']


class Device(models.Model):
    """Registered POS devices and their authentication tokens."""
    device_id = models.CharField(max_length=50, unique=True, primary_key=True)
    sync_token = models.CharField(max_length=255, unique=True)
    business_uuid = models.UUIDField()
    store_uuid = models.UUIDField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)


class ProcessedDeviceChange(models.Model):
    """Idempotency log. Every (device_id, local_seq) recorded once."""
    STATUS_CHOICES = [('applied', 'Applied'), ('conflict', 'Conflict'), ('ignored', 'Ignored')]

    device_id = models.CharField(max_length=50)
    local_seq = models.PositiveIntegerField()
    model = models.CharField(max_length=50)
    object_uuid = models.UUIDField()
    op = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['device_id', 'local_seq']
```

### 4.3 Master SyncPushView

Full implementation including FK resolution:

```python
# sync_views.py (master)
import json
from decimal import Decimal
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from .models import ServerChange, ProcessedDeviceChange
from .serializers import SyncPushRequestSerializer
from .authentication import DeviceTokenAuthentication
# Import all your sync models
from .models import DrawerSession, Order, OrderItem, Transaction, Payment, ReasonCode


MODEL_REGISTRY = {
    'drawer_session': DrawerSession,
    'order': Order,
    'order_item': OrderItem,
    'transaction': Transaction,
    'payment': Payment,
    'reason_code': ReasonCode,
    # add more models here
}

MONEY_FIELDS = {
    'price', 'subtotal', 'total', 'total_tax', 'total_discount',
    'amount', 'change_amount', 'overpayment_amount', 'exchange_rate',
}


@method_decorator(csrf_exempt, name='dispatch')
class SyncPushView(APIView):
    authentication_classes = [DeviceTokenAuthentication]

    def post(self, request):
        serializer = SyncPushRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        if request.user and hasattr(request.user, 'device_id'):
            device_id = request.user.device_id
        else:
            device_id = data['device_id']

        schema_version = data.get('schema_version', 1)
        if schema_version != 1:
            return Response({'error': f'Unsupported schema_version: {schema_version}'},
                            status=status.HTTP_400_BAD_REQUEST)

        last_acked_local_seq = data['last_acked_local_seq']
        changes = data['changes']
        results = []
        conflicts = []
        acked_up_to_local_seq = last_acked_local_seq

        with transaction.atomic():
            for change in changes:
                local_seq = change['local_seq']

                existing = ProcessedDeviceChange.objects.filter(
                    device_id=device_id, local_seq=local_seq
                ).first()

                if existing:
                    if existing.status == 'applied':
                        acked_up_to_local_seq = local_seq
                        results.append({'local_seq': local_seq, 'status': 'applied', 'reason': None})
                        continue
                    if existing.status == 'conflict':
                        results.append({'local_seq': local_seq, 'status': 'conflict',
                                        'reason': existing.result.get('reason') if existing.result else None})
                        conflicts.append(existing.result)
                        break

                result = self._apply_change(change, device_id)

                if result and result.get('status') == 'conflict':
                    results.append({'local_seq': local_seq, 'status': 'conflict',
                                    'reason': result.get('reason')})
                    conflicts.append(result)
                    break
                else:
                    acked_up_to_local_seq = local_seq
                    results.append({'local_seq': local_seq, 'status': 'applied', 'reason': None})

            latest = ServerChange.objects.order_by('-seq').first()
            server_seq = latest.seq if latest else 0

        return Response({
            'acked_up_to_local_seq': acked_up_to_local_seq,
            'results': results,
            'conflicts': conflicts,
            'server_seq': server_seq
        })

    def _apply_change(self, change, device_id):
        model_name = change['model']
        object_uuid = change['object_uuid']
        op = change['op']
        payload = change['payload']
        local_seq = change['local_seq']

        model_class = MODEL_REGISTRY.get(model_name)
        if not model_class:
            return {
                'local_seq': local_seq,
                'object_uuid': str(object_uuid),
                'model': model_name,
                'status': 'conflict',
                'reason': f'Unknown model: {model_name}',
                'server_object': None
            }

        if op == 'delete':
            return self._apply_delete(model_class, object_uuid, local_seq, payload, device_id, model_name)
        else:
            return self._apply_upsert(model_class, model_name, object_uuid, local_seq, payload, device_id)

    def _apply_delete(self, model_class, object_uuid, local_seq, payload, device_id, model_name):
        try:
            obj = model_class.objects.get(uuid=object_uuid)
            obj.soft_delete(syncing_from_device=True)
            self._record_server_change(model_name, object_uuid, 'delete', payload, device_id)
        except model_class.DoesNotExist:
            pass

        ProcessedDeviceChange.objects.create(
            device_id=device_id, local_seq=local_seq, model=model_name,
            object_uuid=object_uuid, op='delete', status='applied', result=None
        )
        return None

    def _apply_upsert(self, model_class, model_name, object_uuid, local_seq, payload, device_id):
        try:
            existing = model_class.objects.get(uuid=object_uuid)
            return self._handle_update(existing, model_name, payload, local_seq, device_id)
        except model_class.DoesNotExist:
            return self._create_object(model_class, model_name, object_uuid, payload, local_seq, device_id)

    def _handle_update(self, existing, model_name, payload, local_seq, device_id):
        """Per-model conflict rules for updates."""
        object_uuid = existing.uuid

        if model_name == 'transaction':
            # Transactions are append-only: reject if data differs
            current = self._serialize_object(existing)
            if not self._equivalent(current, payload):
                return self._conflict(local_seq, object_uuid, model_name,
                                      'Transaction exists with different data', current, device_id)
            # Idempotent re-send
            ProcessedDeviceChange.objects.create(
                device_id=device_id, local_seq=local_seq, model=model_name,
                object_uuid=object_uuid, op='upsert', status='applied', result=None
            )
            return None

        if model_name == 'order':
            if existing.is_closed:
                current = self._serialize_object(existing)
                if not self._equivalent(current, payload):
                    return self._conflict(local_seq, object_uuid, model_name,
                                          'Order is closed and payload differs', current, device_id)

        # Default: version-based conflict for products and other models
        incoming_version = payload.get('version', 1)
        if incoming_version < existing.version:
            return self._conflict(local_seq, object_uuid, model_name,
                                  'Server version is newer', self._serialize_object(existing), device_id)

        self._update_object(existing, model_name, payload, device_id)
        ProcessedDeviceChange.objects.create(
            device_id=device_id, local_seq=local_seq, model=model_name,
            object_uuid=object_uuid, op='upsert', status='applied', result=None
        )
        return None

    def _create_object(self, model_class, model_name, object_uuid, payload, local_seq, device_id):
        try:
            dep_error = self._validate_dependencies(model_name, payload)
            if dep_error:
                return self._conflict(local_seq, object_uuid, model_name, dep_error, None, device_id)

            clean = {k: v for k, v in payload.items()
                     if k not in ('uuid', 'created_at', 'updated_at')}

            # Resolve UUID references → FK IDs
            self._resolve_foreign_keys(model_name, clean)

            # Convert money fields
            for field in list(clean.keys()):
                if field in MONEY_FIELDS and clean[field] is not None:
                    clean[field] = Decimal(str(clean[field]))

            obj = model_class(uuid=object_uuid, **clean)
            obj._syncing_from_device = True
            obj.full_clean()
            obj.save()

            self._record_server_change(model_name, object_uuid, 'upsert',
                                       self._serialize_object(obj), device_id)
            ProcessedDeviceChange.objects.create(
                device_id=device_id, local_seq=local_seq, model=model_name,
                object_uuid=object_uuid, op='upsert', status='applied', result=None
            )
            return None

        except Exception as e:
            return self._conflict(local_seq, object_uuid, model_name,
                                  f'Validation error: {e}', None, device_id)

    def _update_object(self, obj, model_name, payload, device_id):
        exclude = {'uuid', 'created_at', 'updated_at', 'deleted_at'}
        clean = {k: v for k, v in payload.items() if k not in exclude}
        self._resolve_foreign_keys(model_name, clean)

        for field, value in clean.items():
            if hasattr(obj, field):
                if field in MONEY_FIELDS and value is not None:
                    value = Decimal(str(value))
                setattr(obj, field, value)

        obj._syncing_from_device = True
        obj.version = payload.get('version', obj.version)
        obj.save()
        self._record_server_change(model_name, obj.uuid, 'upsert',
                                   self._serialize_object(obj), device_id)

    def _resolve_foreign_keys(self, model_name, payload):
        """
        Translate *_uuid fields into Django FK _id fields.
        Add a block for every model that has FKs.
        """
        fk_map = FK_RESOLUTION_MAP.get(model_name, [])
        for uuid_field, fk_field, related_model in fk_map:
            uuid_val = payload.pop(uuid_field, None)
            if uuid_val:
                try:
                    obj = related_model.objects.get(uuid=uuid_val)
                    payload[fk_field] = obj.pk
                except related_model.DoesNotExist:
                    payload[fk_field] = None
            else:
                payload[fk_field] = None

    def _validate_dependencies(self, model_name, payload):
        """
        Check required parent objects exist before creating a child.
        Add required=True for mandatory FKs.
        """
        rules = DEPENDENCY_RULES.get(model_name, [])
        for uuid_field, related_model, required in rules:
            uuid_val = payload.get(uuid_field)
            if uuid_val and not related_model.objects.filter(uuid=uuid_val).exists():
                if required:
                    return f'Missing {related_model.__name__}: {uuid_field}={uuid_val}'
        return None

    def _conflict(self, local_seq, object_uuid, model_name, reason, server_object, device_id):
        result = {
            'local_seq': local_seq,
            'object_uuid': str(object_uuid),
            'model': model_name,
            'status': 'conflict',
            'reason': reason,
            'server_object': server_object
        }
        ProcessedDeviceChange.objects.create(
            device_id=device_id, local_seq=local_seq, model=model_name,
            object_uuid=object_uuid, op='upsert', status='conflict', result=result
        )
        return result

    def _equivalent(self, p1, p2):
        ignore = {'created_at', 'updated_at', 'version'}
        return {k: v for k, v in p1.items() if k not in ignore} == \
               {k: v for k, v in p2.items() if k not in ignore}

    def _serialize_object(self, obj):
        from .serializers import get_serializer_for_model
        serializer_class = get_serializer_for_model(obj.__class__)
        if serializer_class:
            return serializer_class(obj).data
        return {}

    def _record_server_change(self, model, object_uuid, op, payload, device_id):
        ServerChange.objects.create(
            model=model, device_id=device_id,
            object_uuid=object_uuid, op=op, payload=payload
        )
```

### 4.4 FK Resolution Map and Dependency Rules

Define these as module-level dictionaries near the top of `sync_views.py`. Update when you add models:

```python
# sync_views.py (master) — put these near the top after imports

from django.contrib.auth import get_user_model

# FK_RESOLUTION_MAP:
# model_name → list of (uuid_field_in_payload, fk_id_field_on_model, related_model_class)
FK_RESOLUTION_MAP = {
    'transaction': [
        ('drawer_session_uuid',  'drawer_session_id',  DrawerSession),
        ('created_by_uuid',      'created_by_id',      get_user_model()),
        ('related_order_uuid',   'related_order_id',   Order),
        ('original_order_uuid',  'original_order_id',  Order),
        ('reason_code_uuid',     'reason_code_id',     ReasonCode),
    ],
    'order_item': [
        ('order_uuid',    'order_id',    Order),
        ('product_uuid',  'product_id',  Product),
    ],
    'payment': [
        ('transaction_uuid',  'transaction_id',  Transaction),
        ('order_uuid',        'order_id',        Order),
    ],
    # add more models here
}

# DEPENDENCY_RULES:
# model_name → list of (uuid_field_in_payload, related_model_class, required:bool)
# required=True → if parent missing, the change is marked as conflict (not silently nulled)
DEPENDENCY_RULES = {
    'transaction': [
        ('related_order_uuid', Order, False),     # optional — OK if order not yet on master
        ('drawer_session_uuid', DrawerSession, False),
    ],
    'order_item': [
        ('order_uuid', Order, True),   # required — order MUST exist before items
        ('product_uuid', Product, False),
    ],
    'payment': [
        ('transaction_uuid', Transaction, True),  # required
    ],
}
```

### 4.5 Authentication

```python
# authentication.py (master)
from rest_framework import authentication, exceptions
from .models import Device
from django.utils import timezone


class DeviceTokenAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return None
        token = auth_header.replace('Bearer ', '').strip()
        if not token:
            return None
        try:
            device = Device.objects.get(sync_token=token, is_active=True)
            device.last_sync_at = timezone.now()
            device.save(update_fields=['last_sync_at'])
            return (device, None)
        except Device.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid or inactive device token')

    def authenticate_header(self, request):
        return 'Bearer'
```

### 4.6 Master URL Configuration

```python
# master/urls.py
from django.urls import path
from .sync_views import SyncPushView, SyncPullView

urlpatterns = [
    path('sync/push/', SyncPushView.as_view(), name='sync_push'),
    path('sync/pull/', SyncPullView.as_view(), name='sync_pull'),
]

# root urls.py
urlpatterns = [
    path('master/', include('master.urls')),
    # ... your other URLs
]
```

---

## 5. Communication Layer

### 5.1 Push Request (Local → Master)

```
POST /master/sync/push/
Content-Type: application/json
Authorization: Bearer <sync_token>
```

```json
{
  "device_id": "POS-1",
  "last_acked_local_seq": 100,
  "schema_version": 1,
  "business_uuid": "biz-uuid-here",
  "store_uuid": "store-uuid-here",
  "changes": [
    {
      "local_seq": 101,
      "model": "order",
      "object_uuid": "aaa-bbb-ccc",
      "op": "upsert",
      "payload": { ... },
      "ts": "2026-06-25T08:30:00+05:00"
    },
    {
      "local_seq": 102,
      "model": "transaction",
      "object_uuid": "ddd-eee-fff",
      "op": "upsert",
      "payload": { ... },
      "ts": "2026-06-25T08:30:01+05:00"
    }
  ]
}
```

### 5.2 Push Response (Master → Local)

```json
{
  "acked_up_to_local_seq": 102,
  "results": [
    {"local_seq": 101, "status": "applied", "reason": null},
    {"local_seq": 102, "status": "applied", "reason": null}
  ],
  "conflicts": [],
  "server_seq": 450
}
```

**Conflict example:**
```json
{
  "acked_up_to_local_seq": 101,
  "results": [
    {"local_seq": 101, "status": "applied", "reason": null},
    {"local_seq": 102, "status": "conflict", "reason": "Missing Order: related_order_uuid=xxx does not exist"}
  ],
  "conflicts": [
    {
      "local_seq": 102,
      "object_uuid": "ddd-eee-fff",
      "model": "transaction",
      "status": "conflict",
      "reason": "Missing Order: related_order_uuid=xxx does not exist",
      "server_object": null
    }
  ],
  "server_seq": 449
}
```

### 5.3 Flow Diagram (Per Sync Cycle)

```
LOCAL                                          MASTER
  │                                               │
  │  1. Read DeviceChange rows (pushed=False)     │
  │     ordered by local_seq, limit 200           │
  │                                               │
  │  2. POST /master/sync/push/ ─────────────────►│
  │                                               │  3. Validate schema_version
  │                                               │  4. For each change in order:
  │                                               │     a. Check ProcessedDeviceChange (idempotent)
  │                                               │     b. Validate dependencies (parent UUIDs exist)
  │                                               │     c. Resolve *_uuid → FK _id
  │                                               │     d. Apply upsert or soft_delete
  │                                               │     e. Write ServerChange log
  │                                               │     f. Write ProcessedDeviceChange
  │                                               │     g. If conflict: stop, return conflict
  │                                               │
  │◄──────────────── Response ────────────────────│
  │                                               │
  │  5. Update SyncState.last_acked_local_seq      │
  │  6. Mark DeviceChange rows pushed=True        │
  │  7. Store any conflicts in LocalConflict      │
  │                                               │
  │  8. Sleep 3 seconds → repeat                  │
```

---

## 6. Foreign Keys + UUID Pattern — The Complete Rule

### Rule Summary

| Layer | What to store | What to send |
|---|---|---|
| **Local model** | UUID field + FK field (optional FK if related model exists locally) | Only UUID fields in payload |
| **Sync payload** | — | `*_uuid` fields only, never `*_id` |
| **Master model** | UUID field + FK field + integer id | Resolved FK after receiving |
| **Master resolution** | — | Look up FK object by UUID, set `fk_id` |

### Local Model Pattern

```python
class Transaction(SyncBaseModel):
    # --- scalar fields ---
    transaction_number = models.CharField(max_length=10, unique=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)

    # --- FK pair: UUID field (synced) + FK field (local ORM use) ---
    drawer_session_uuid = models.UUIDField(null=True, blank=True)
    drawer_session = models.ForeignKey(DrawerSession, on_delete=models.SET_NULL,
                                        null=True, blank=True)

    related_order_uuid = models.UUIDField(null=True, blank=True)
    related_order = models.ForeignKey('orders.Order', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='order_transaction')
```

### Master Model Pattern

```python
class Transaction(SyncBaseModel):
    # SyncBaseModel gives: id (BigAutoField PK), uuid (unique), version, timestamps

    # --- scalar fields ---
    transaction_number = models.CharField(max_length=10, unique=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)

    # --- FK pair: UUID field (sync identity) + FK field (master ORM use) ---
    drawer_session_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    drawer_session = models.ForeignKey(DrawerSession, on_delete=models.SET_NULL,
                                        null=True, blank=True)

    related_order_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    related_order = models.ForeignKey('orders.Order', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='order_transaction')
```

---

## 7. Full Example: Transaction Model with 5 Foreign Keys

This section shows the complete implementation for a real `Transaction` model with `DrawerSession`, `User`, `Order` (x2), and `ReasonCode` FK dependencies.

### 7.1 Local Model

```python
# local models.py
from apps.store.pos.utils import generate_transaction_number


class Transaction(SyncBaseModel):
    TRANSACTION_TYPES = [
        ('open', 'Open'), ('close', 'Close'), ('order', 'Order'),
        ('return', 'Return'), ('cancel', 'Cancel'), ('deposit', 'Deposit'),
        ('withdrawal', 'Withdrawal'), ('change_payment', 'Change Payment'),
    ]
    ORDER_TRANSACTION_TYPES = [
        ('Receipt', 'Receipt'), ('Invoice', 'Invoice'),
    ]

    transaction_number = models.CharField(
        max_length=10, unique=True, default=generate_transaction_number, editable=False
    )
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    order_transaction_type = models.CharField(max_length=20, choices=ORDER_TRANSACTION_TYPES,
                                               null=True, blank=True)
    note = models.TextField(blank=True, null=True)
    receipt_id = models.CharField(max_length=255, null=True, blank=True)
    ap_number = models.CharField(max_length=50, null=True, blank=True)
    printer_xml_response = models.TextField(blank=True, null=True)
    reason = models.CharField(max_length=100, null=True, blank=True)

    # FK 1: DrawerSession
    drawer_session_uuid = models.UUIDField(null=True, blank=True)
    drawer_session = models.ForeignKey(
        DrawerSession, on_delete=models.SET_NULL, null=True, blank=True
    )

    # FK 2: User (created_by)
    created_by_uuid = models.UUIDField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    # FK 3: Order (the order this transaction belongs to)
    related_order_uuid = models.UUIDField(null=True, blank=True)
    related_order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='order_transaction'
    )

    # FK 4: Order (original order for return transactions)
    original_order_uuid = models.UUIDField(null=True, blank=True)
    original_order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='original_order_transaction'
    )

    # FK 5: ReasonCode
    reason_code_uuid = models.UUIDField(null=True, blank=True)
    reason_code = models.ForeignKey(
        'orders.ReasonCode', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions'
    )
```

### 7.2 Master Model

Identical FK pair pattern, but `SyncBaseModel` gives `id` (BigAutoField PK) + `uuid` (unique):

```python
# master models.py
class Transaction(SyncBaseModel):
    # All same fields as local, plus db_index=True on UUID FK fields for fast lookups
    transaction_number = models.CharField(max_length=10, unique=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    order_transaction_type = models.CharField(max_length=20, choices=ORDER_TRANSACTION_TYPES,
                                               null=True, blank=True)
    note = models.TextField(blank=True, null=True)
    receipt_id = models.CharField(max_length=255, null=True, blank=True)
    ap_number = models.CharField(max_length=50, null=True, blank=True)
    printer_xml_response = models.TextField(blank=True, null=True)
    reason = models.CharField(max_length=100, null=True, blank=True)

    drawer_session_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    drawer_session = models.ForeignKey(
        DrawerSession, on_delete=models.SET_NULL, null=True, blank=True
    )

    created_by_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    related_order_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    related_order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='order_transaction'
    )

    original_order_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    original_order = models.ForeignKey(
        'orders.Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='original_order_transaction'
    )

    reason_code_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    reason_code = models.ForeignKey(
        'orders.ReasonCode', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions'
    )
```

### 7.3 What the Payload Looks Like

This is exactly what gets stored in `DeviceChange.payload` and sent to master:

```json
{
  "uuid": "f1a2b3c4-d5e6-7890-abcd-ef1234567890",
  "transaction_number": "STORE1-0001",
  "transaction_type": "order",
  "order_transaction_type": "Receipt",
  "note": null,
  "receipt_id": "REC-001",
  "ap_number": null,
  "printer_xml_response": null,
  "reason": null,

  "drawer_session_uuid": "aa11bb22-cc33-dd44-ee55-ff6677889900",
  "created_by_uuid": "11111111-2222-3333-4444-555555555555",
  "related_order_uuid": "aaaabbbb-cccc-dddd-eeee-ffffaaaabbbb",
  "original_order_uuid": null,
  "reason_code_uuid": null,

  "created_at": "2026-06-25T08:30:00+05:00",
  "updated_at": "2026-06-25T08:30:00+05:00",
  "deleted_at": null,
  "version": 1
}
```

**Note:** No `drawer_session_id`, `created_by_id`, `related_order_id` — only UUID fields travel.

### 7.4 Master FK Resolution for Transaction

```python
# In FK_RESOLUTION_MAP (sync_views.py on master)
'transaction': [
    ('drawer_session_uuid',  'drawer_session_id',  DrawerSession),
    ('created_by_uuid',      'created_by_id',      get_user_model()),
    ('related_order_uuid',   'related_order_id',   Order),
    ('original_order_uuid',  'original_order_id',  Order),
    ('reason_code_uuid',     'reason_code_id',     ReasonCode),
],
```

What `_resolve_foreign_keys` does step by step:
1. Reads `drawer_session_uuid` from payload
2. Queries `DrawerSession.objects.get(uuid=drawer_session_uuid)`
3. Gets back the `DrawerSession` object (which has its own `id` on master)
4. Sets `payload['drawer_session_id'] = drawer_session.id`
5. Removes `drawer_session_uuid` from payload (already stored as its own column)
6. Repeats for all 5 FK pairs
7. Model is saved with all FK IDs correctly populated — both UUID column and FK column are filled

After save, `Transaction` on master has:
- `drawer_session_uuid` = `"aa11bb22..."` (stored, queryable)
- `drawer_session_id` = `42` (master internal ID, FK intact)
- Both are set. ORM relations work. Sync queries by UUID work.

---

## 8. Multi-Model API (4–5 models in 1 API call)

### 8.1 The Key Insight

Your APIs do not need to know about sync at all. They write to local models. Signals automatically create `DeviceChange` rows in order. The sync loop ships them in `local_seq` order. Master applies them in order.

**The dependency order guarantee:** Because all writes happen in `transaction.atomic()`, and signals fire in the order models are saved, and `local_seq` is an auto-increment, the order is preserved automatically.

### 8.2 Example: Complete Order Checkout API

This single API creates DrawerSession update + Order + 2 OrderItems + Transaction + Payment (6 models):

```python
# local views.py
from django.db import transaction as db_transaction

@db_transaction.atomic()
def checkout(request):
    data = request.data

    # 1. Update DrawerSession (parent of Transaction)
    drawer_session = DrawerSession.objects.get(uuid=data['drawer_session_uuid'])
    drawer_session.total_sales += data['total']
    drawer_session.save()
    # → DeviceChange(seq=101, model='drawer_session', op='upsert')

    # 2. Create/update Order
    order = Order.objects.create(
        uuid=uuid.uuid4(),
        order_number=generate_order_number(),
        state='order',
        is_closed=True,
        total_tax=data['tax'],
        total_discount=data['discount'],
        store_id=data['store_id'],
    )
    # → DeviceChange(seq=102, model='order', op='upsert')

    # 3. Create OrderItems
    for item_data in data['items']:
        product = Product.objects.get(uuid=item_data['product_uuid'])
        OrderItem.objects.create(
            uuid=uuid.uuid4(),
            order_uuid=order.uuid,       # UUID companion field
            order=order,                  # FK
            product_uuid=product.uuid,   # UUID companion field
            product=product,              # FK
            quantity=item_data['quantity'],
            unit_price=item_data['price'],
        )
        # → DeviceChange(seq=103, model='order_item', op='upsert')
        # → DeviceChange(seq=104, model='order_item', op='upsert')

    # 4. Create Transaction
    txn = Transaction.objects.create(
        uuid=uuid.uuid4(),
        transaction_number=generate_transaction_number(),
        transaction_type='order',
        order_transaction_type='Receipt',
        drawer_session_uuid=drawer_session.uuid,  # UUID companion
        drawer_session=drawer_session,              # FK
        created_by_uuid=request.user.uuid,         # UUID companion
        created_by=request.user,                   # FK
        related_order_uuid=order.uuid,             # UUID companion
        related_order=order,                       # FK
    )
    # → DeviceChange(seq=105, model='transaction', op='upsert')

    # 5. Create Payment
    payment = Payment.objects.create(
        uuid=uuid.uuid4(),
        transaction_uuid=txn.uuid,   # UUID companion
        transaction=txn,              # FK
        order_uuid=order.uuid,        # UUID companion
        order=order,                  # FK
        method='cash',
        amount=data['amount_tendered'],
        change_amount=data['change'],
    )
    # → DeviceChange(seq=106, model='payment', op='upsert')

    return Response({'order_uuid': str(order.uuid), 'transaction_uuid': str(txn.uuid)})
```

### 8.3 What Gets Sent to Master

```json
{
  "device_id": "POS-1",
  "last_acked_local_seq": 100,
  "schema_version": 1,
  "changes": [
    {"local_seq": 101, "model": "drawer_session", "op": "upsert", "payload": {...}},
    {"local_seq": 102, "model": "order",          "op": "upsert", "payload": {...}},
    {"local_seq": 103, "model": "order_item",     "op": "upsert", "payload": {"order_uuid": "...", ...}},
    {"local_seq": 104, "model": "order_item",     "op": "upsert", "payload": {"order_uuid": "...", ...}},
    {"local_seq": 105, "model": "transaction",    "op": "upsert", "payload": {"related_order_uuid": "...", ...}},
    {"local_seq": 106, "model": "payment",        "op": "upsert", "payload": {"transaction_uuid": "...", ...}}
  ]
}
```

Master processes them in order. By the time `transaction` arrives, `order` already exists. By the time `payment` arrives, `transaction` already exists. FK resolution works cleanly.

### 8.4 Ordering Rule

Always save models in parent-before-child order within `transaction.atomic()`:

```
DrawerSession (root)
  └── Order
        ├── OrderItem (depends on Order + Product)
        └── Transaction (depends on Order + DrawerSession)
              └── Payment (depends on Transaction)
```

---

## 9. Scaling to 100–200 APIs

### Why it scales

| Concern | Answer |
|---|---|
| One endpoint per API? | **No.** One `/master/sync/push/` for all models |
| 200 APIs means 200 sync endpoints? | **No.** Sync is model-based, not API-based |
| What if I add a new API? | Just write to local models — signals handle the rest |
| What if I add a new model? | Add to `signals.py`, `FK_RESOLUTION_MAP`, `MODEL_REGISTRY`, `DEPENDENCY_RULES` |
| Batch throughput | 200 changes/batch × 3-second interval = ~4,000 changes/minute |
| Idempotency | Every `(device_id, local_seq)` pair is stored in `ProcessedDeviceChange` |

### What you do when adding a new API

1. Write your local API normally using `transaction.atomic()`
2. Save models in parent-before-child order
3. Always set both `*_uuid` and FK fields when creating objects
4. **Nothing else.** The sync system picks it up automatically.

### What you do when adding a new sync model

See [Section 11](#11-adding-a-new-sync-model--checklist).

---

## 10. Conflict Resolution Rules

| Model | Rule | Behaviour |
|---|---|---|
| **Product** | Version-based | Reject if `incoming_version < server_version` |
| **Order** | Lock-based | Reject update if `is_closed=True` and payload differs |
| **Transaction** | Append-only | Reject if UUID exists with different data |
| **OrderItem** | Version-based | Same as Product |
| **Payment** | Append-only | Same as Transaction |

### What happens on conflict

1. Master stops processing the batch at the conflicting `local_seq`
2. Returns `conflicts[]` array with reason and server's current state
3. Local stores it in `LocalConflict` table
4. `DeviceChange` row is NOT marked `pushed=True`
5. Will be re-sent next cycle unless manually resolved

### Conflict Resolution on Local

```python
# To resolve a conflict manually:
conflict = LocalConflict.objects.get(device_id='POS-1', local_seq=102)
conflict.resolved = True
conflict.save()

# Mark the DeviceChange as pushed to skip it
DeviceChange.objects.filter(local_seq=102).update(pushed=True)

# Or re-create the object with corrected data and let it re-sync
```

---

## 11. Adding a New Sync Model — Checklist

Use this checklist every time you add a new model to the sync system.

### Local side

- [ ] Model inherits from `SyncBaseModel`
- [ ] Every FK has a companion `*_uuid = models.UUIDField(null=True, blank=True)`
- [ ] Set both `*_uuid` and FK when saving (`obj.related_order_uuid = order.uuid; obj.related_order = order`)
- [ ] Add model class → string name to `get_model_name()` in `signals.py`
- [ ] Add `@receiver(post_save, sender=YourModel)` to `handle_model_save` in `signals.py`
- [ ] Run `makemigrations` and `migrate`

### Master side

- [ ] Model inherits from master `SyncBaseModel` (has both `id` and `uuid`)
- [ ] Every FK has companion `*_uuid = models.UUIDField(null=True, blank=True, db_index=True)`
- [ ] Add to `MODEL_REGISTRY` in `sync_views.py`
- [ ] Add to `FK_RESOLUTION_MAP` in `sync_views.py`
- [ ] Add to `DEPENDENCY_RULES` in `sync_views.py` (for required parents)
- [ ] Add `ModelSerializer` in `serializers.py` and register in `get_serializer_for_model()`
- [ ] Add model string to `SyncChangeSerializer` choices list
- [ ] Run `makemigrations` and `migrate`

### Testing

- [ ] Create the model locally → verify `DeviceChange` row created
- [ ] Run sync loop → verify master receives and applies
- [ ] Verify master model has FK populated (not just UUID)
- [ ] Test idempotency: send same change twice → second is applied without error
- [ ] Test missing parent: send child without parent → conflict returned

---

## 12. Running the System

### Start Master

```bash
cd sync_django_master
python manage.py runserver 0.0.0.0:8000
```

### Register a Device on Master

```python
# In master Django shell or admin
from master.models import Device
import secrets

Device.objects.create(
    device_id='POS-1',
    sync_token=secrets.token_hex(32),
    business_uuid='your-business-uuid',
    store_uuid='your-store-uuid',
    is_active=True
)
```

### Configure Local Device

```python
# In local Django shell or admin
from master.models import LocalDeviceConfig

LocalDeviceConfig.objects.create(
    device_id='POS-1',
    sync_token='<token from master Device record>',
    master_url='http://localhost:8000',  # or production URL
    business_uuid='your-business-uuid',
    store_uuid='your-store-uuid',
    sync_enabled=True
)
```

### Start Local Server

```bash
cd sync_django_local
python manage.py runserver 8001
```

### Start Sync Loop

```bash
cd sync_django_local
python manage.py sync_loop --device-id POS-1 --sleep-interval 3
```

### Flutter / Serial Python

Flutter calls `http://localhost:8001` (local Django). The sync loop runs in the background as a separate process. Flutter never talks to master directly.

---

## Appendix: Serializer for `get_serializer_for_model`

```python
# serializers.py (master)
from rest_framework import serializers
from .models import DrawerSession, Order, OrderItem, Transaction, Payment, ReasonCode


class DrawerSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DrawerSession
        fields = ['uuid', 'store_id', 'opened_at', 'closed_at',
                  'total_sales', 'created_at', 'updated_at', 'deleted_at', 'version']


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ['uuid', 'order_number', 'store_id', 'state', 'is_closed',
                  'total_tax', 'total_discount', 'created_at', 'updated_at', 'deleted_at', 'version']


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            'uuid', 'transaction_number', 'transaction_type', 'order_transaction_type',
            'note', 'receipt_id', 'ap_number', 'reason',
            'drawer_session_uuid', 'created_by_uuid',
            'related_order_uuid', 'original_order_uuid', 'reason_code_uuid',
            'created_at', 'updated_at', 'deleted_at', 'version'
        ]


SERIALIZER_MAP = {
    DrawerSession: DrawerSessionSerializer,
    Order: OrderSerializer,
    Transaction: TransactionSerializer,
    # add others
}


def get_serializer_for_model(model_class):
    return SERIALIZER_MAP.get(model_class)
```

> **Important:** Serializers must list `*_uuid` fields, not `*_id` fields. The serialized output is what goes into `ServerChange.payload` and travels over the wire.
