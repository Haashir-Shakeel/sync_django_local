import time
import json
import os
import requests
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone
from django.db import transaction, IntegrityError
from master.models import DeviceChange, SyncState, LocalConflict, Product, Order, Transaction, SyncLock, LocalDeviceConfig


class Command(BaseCommand):
    help = 'Sync loop for POS device to sync with master server'

    def add_arguments(self, parser):
        parser.add_argument(
            '--master',
            type=str,
            default=getattr(settings, 'MASTER_URL', 'http://localhost:8000'),
            help='Master server URL (default: http://localhost:8000)'
        )
        parser.add_argument(
            '--device-id',
            type=str,
            default=getattr(settings, 'DEVICE_ID', 'POS-1'),
            help='Device ID for this POS (default: POS-1)'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=200,
            help='Batch size for pushing changes (default: 200)'
        )
        parser.add_argument(
            '--sleep-interval',
            type=int,
            default=3,
            help='Sleep interval between sync cycles in seconds (default: 3)'
        )

    def handle(self, *args, **options):
        master_url = options['master'].rstrip('/')
        device_id = options['device_id']
        batch_size = options['batch_size']
        sleep_interval = options['sleep_interval']

        self.stdout.write(
            self.style.SUCCESS(f'Starting sync loop for device {device_id} with master {master_url}')
        )

        # Retrieve device configuration for authentication and P0 payload fields
        try:
            device_config = LocalDeviceConfig.objects.get(device_id=device_id)
            if not device_config.sync_enabled:
                self.stdout.write(
                    self.style.WARNING(f'Sync is disabled for device {device_id}')
                )
                return
            
            # Use config values if available
            if device_config.master_url:
                master_url = device_config.master_url.rstrip('/')
                self.stdout.write(f'Using master URL from config: {master_url}')
            
            sync_token = device_config.sync_token
            business_uuid = device_config.business_uuid
            store_uuid = device_config.store_uuid
            self.stdout.write(f'Using authentication token from device config')
            self.stdout.write(f'Business UUID: {business_uuid}, Store UUID: {store_uuid}')
            
        except LocalDeviceConfig.DoesNotExist:
            self.stdout.write(
                self.style.WARNING(
                    f'No LocalDeviceConfig found for device {device_id}. '
                    f'Sync will proceed without authentication (insecure). '
                    f'Please create a LocalDeviceConfig record with sync_token.'
                )
            )
            sync_token = None
            business_uuid = None
            store_uuid = None

        # Acquire lock to ensure singleton sync loop
        lock_acquired = self.acquire_lock(device_id)
        if not lock_acquired:
            return  # Exit if lock cannot be acquired

        # Ensure SyncState exists
        sync_state, created = SyncState.objects.get_or_create(
            device_id=device_id,
            defaults={
                'last_acked_local_seq': 0,
                'last_server_seq': 0,
                'last_sync_ts': None
            }
        )

        if created:
            self.stdout.write(f'Created new SyncState for device {device_id}')

        try:
            while True:
                try:
                    # Push local changes to master
                    self.push_changes(master_url, device_id, batch_size, sync_state, sync_token, business_uuid, store_uuid)
                    
                    # Pull changes from master (commented out for now - push only)
                    # self.pull_changes(master_url, device_id, sync_state, sync_token)
                    
                    # Update last sync timestamp
                    sync_state.last_sync_ts = timezone.now()
                    sync_state.save()
                    
                    self.stdout.write(f'Sync cycle completed. Sleeping for {sleep_interval} seconds...')
                    time.sleep(sleep_interval)
                    
                except requests.RequestException as e:
                    self.stdout.write(
                        self.style.ERROR(f'Network error during sync: {e}. Retrying in {sleep_interval} seconds...')
                    )
                    time.sleep(sleep_interval)
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f'Unexpected error during sync: {e}. Retrying in {sleep_interval} seconds...')
                    )
                    time.sleep(sleep_interval)
                    
        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS('Sync loop stopped by user'))
        finally:
            # Release lock on exit
            self.release_lock(device_id)
            self.stdout.write(self.style.SUCCESS('Released sync lock'))

    def push_changes(self, master_url, device_id, batch_size, sync_state, sync_token, business_uuid=None, store_uuid=None):
        """Push local changes to master server with P0 standard payload format"""
        # Get unpushed changes - convert to list to avoid lazy query evaluation
        unpushed_changes = list(
            DeviceChange.objects.filter(
                device_id=device_id,
                local_seq__gt=sync_state.last_acked_local_seq,
                pushed=False
            ).order_by('local_seq')[:batch_size]
        )

        if not unpushed_changes:
            return

        # Prepare payload
        changes_data = []
        for change in unpushed_changes:
            changes_data.append({
                'local_seq': change.local_seq,
                'model': change.model,
                'object_uuid': str(change.object_uuid),
                'op': change.op,
                'payload': change.payload,
                'ts': change.ts.isoformat()
            })

        # P0: Standard sync payload with schema_version and business/store UUIDs
        payload = {
            'device_id': device_id,
            'last_acked_local_seq': sync_state.last_acked_local_seq,
            'schema_version': 1,
            'changes': changes_data
        }
        
        # Add business_uuid and store_uuid if available
        if business_uuid:
            payload['business_uuid'] = str(business_uuid)
        if store_uuid:
            payload['store_uuid'] = str(store_uuid)

        self.stdout.write(f'Pushing {len(changes_data)} changes to master...')

        try:
            # Mark attempt on all changes in this batch
            for change in unpushed_changes:
                change.attempt_count += 1
                change.last_attempt_at = timezone.now()
                change.save(update_fields=['attempt_count', 'last_attempt_at'])
            
            # Prepare headers with authentication
            headers = {'Content-Type': 'application/json'}
            if sync_token:
                # headers['Authorization'] = f'Bearer {sync_token}'
                print("Not sending headers")
            
            response = requests.post(
                f'{master_url}/master/sync/push/',
                json=payload,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            result = response.json()

            # P0: Process results array for detailed per-change status
            if 'results' in result:
                self.stdout.write(f'Received {len(result["results"])} change results from master')
                for change_result in result['results']:
                    local_seq = change_result['local_seq']
                    status = change_result['status']
                    reason = change_result.get('reason')
                    
                    if status == 'applied':
                        self.stdout.write(f'  seq {local_seq}: applied')
                    elif status == 'conflict':
                        self.stdout.write(self.style.WARNING(f'  seq {local_seq}: conflict - {reason}'))
                    elif status == 'error':
                        self.stdout.write(self.style.ERROR(f'  seq {local_seq}: error - {reason}'))

            # Handle conflicts first (before marking as pushed)
            conflicted_seqs = set()
            if 'conflicts' in result and result['conflicts']:
                conflicted_seqs = self.handle_conflicts(device_id, result['conflicts'])

            # Handle successful acknowledgment
            if 'acked_up_to_local_seq' in result:
                acked_seq = result['acked_up_to_local_seq']
                sync_state.last_acked_local_seq = max(sync_state.last_acked_local_seq, acked_seq)
                sync_state.save()
                
                # Mark changes as pushed with timestamp and clear errors
                # Only mark rows that are not already pushed (safe ack)
                # Exclude conflicted rows from being marked as pushed
                query = DeviceChange.objects.filter(
                    device_id=device_id,
                    local_seq__lte=acked_seq,
                    pushed=False
                )
                if conflicted_seqs:
                    query = query.exclude(local_seq__in=conflicted_seqs)
                
                query.update(
                    pushed=True,
                    pushed_at=timezone.now(),
                    last_error=None
                )
                
                self.stdout.write(f'Master acknowledged changes up to seq {acked_seq}')

        except requests.RequestException as e:
            error_msg = str(e)
            self.stdout.write(self.style.ERROR(f'Failed to push changes: {error_msg}'))
            
            # Record error on all changes in this batch
            for change in unpushed_changes:
                change.last_error = error_msg
                change.save(update_fields=['last_error'])
            
            raise
        except Exception as e:
            error_msg = str(e)
            self.stdout.write(self.style.ERROR(f'Unexpected error pushing changes: {error_msg}'))
            
            # Record error on all changes in this batch
            for change in unpushed_changes:
                change.last_error = error_msg
                change.save(update_fields=['last_error'])
            
            raise

    def pull_changes(self, master_url, device_id, sync_state):
        """Pull changes from master server"""
        try:
            response = requests.get(
                f'{master_url}/master/sync/pull/',
                params={'since': sync_state.last_server_seq},
                timeout=30
            )
            response.raise_for_status()
            result = response.json()

            changes = result.get('changes', [])
            if not changes:
                return

            self.stdout.write(f'Pulling {len(changes)} changes from master...')

            # Apply changes locally
            with transaction.atomic():
                for change in changes:
                    self.apply_change(change)
                    sync_state.last_server_seq = max(sync_state.last_server_seq, change['seq'])

                sync_state.save()

            self.stdout.write(f'Applied {len(changes)} changes from master')

        except requests.RequestException as e:
            self.stdout.write(self.style.ERROR(f'Failed to pull changes: {e}'))
            raise

    def apply_change(self, change):
        """Apply a single change from master to local database"""
        model_map = {
            'product': Product,
            'order': Order,
            'transaction': Transaction
        }

        model_class = model_map.get(change['model'])
        if not model_class:
            self.stdout.write(self.style.WARNING(f'Unknown model: {change["model"]}'))
            return

        object_uuid = change['object_uuid']
        
        if change['op'] == 'delete':
            # Handle delete operation
            try:
                instance = model_class.objects.get(uuid=object_uuid)
                # Mark as syncing from master to avoid triggering signals
                instance._syncing_from_master = True
                instance.delete()
                self.stdout.write(f'Deleted {change["model"]} {object_uuid}')
            except model_class.DoesNotExist:
                # Already deleted or never existed locally
                pass
        elif change['op'] == 'upsert':
            # Handle upsert operation
            payload = change['payload'].copy()
            
            # Parse datetime fields from ISO strings
            datetime_fields = ['created_at', 'updated_at', 'deleted_at']
            for field in datetime_fields:
                if field in payload and payload[field]:
                    if isinstance(payload[field], str):
                        from django.utils.dateparse import parse_datetime
                        payload[field] = parse_datetime(payload[field])
            
            # Remove uuid from payload as it's the primary key
            uuid_val = payload.pop('uuid', object_uuid)
            
            # Create or update the instance
            instance, created = model_class.objects.update_or_create(
                uuid=uuid_val,
                defaults=payload
            )
            
            # Mark as syncing from master to avoid triggering signals
            instance._syncing_from_master = True
            
            action = 'Created' if created else 'Updated'
            self.stdout.write(f'{action} {change["model"]} {object_uuid}')

    def handle_conflicts(self, device_id, conflicts):
        """Handle conflicts returned by master (P0 enhanced format)
        
        Returns:
            set: Set of local_seq values that have conflicts
        """
        self.stdout.write(self.style.WARNING(f'Received {len(conflicts)} conflicts from master'))
        
        conflicted_seqs = set()
        for conflict in conflicts:
            local_seq = conflict['local_seq']
            conflicted_seqs.add(local_seq)
            
            # P0: Enhanced conflict format includes model field
            model_name = conflict.get('model', 'unknown')
            
            # Fallback: Get model name from DeviceChange if not in conflict data
            if model_name == 'unknown':
                try:
                    device_change = DeviceChange.objects.get(
                        device_id=device_id,
                        local_seq=local_seq
                    )
                    model_name = device_change.model
                except DeviceChange.DoesNotExist:
                    pass
            
            LocalConflict.objects.update_or_create(
                device_id=device_id,
                local_seq=local_seq,
                defaults={
                    'model': model_name,
                    'object_uuid': conflict['object_uuid'],
                    'conflict_data': conflict,
                    'resolved': False
                }
            )
            
        self.stdout.write(f'Stored {len(conflicts)} conflicts for manual resolution')
        return conflicted_seqs

    def acquire_lock(self, device_id):
        """Acquire sync lock to ensure only one sync loop runs at a time
        
        Returns:
            bool: True if lock acquired, False otherwise
        """
        lock_name = f'sync_loop_{device_id}'
        process_id = os.getpid()
        
        # Check for stale locks (older than 5 minutes without heartbeat)
        stale_threshold = timezone.now() - timezone.timedelta(minutes=5)
        stale_locks = SyncLock.objects.filter(
            lock_name=lock_name,
            last_heartbeat__lt=stale_threshold
        )
        
        if stale_locks.exists():
            self.stdout.write(
                self.style.WARNING(f'Found stale lock, removing it...')
            )
            stale_locks.delete()
        
        # Try to acquire lock
        try:
            with transaction.atomic():
                # Use select_for_update to prevent race conditions
                existing_lock = SyncLock.objects.select_for_update().filter(
                    lock_name=lock_name
                ).first()
                
                if existing_lock:
                    # Check if lock is still active and recent
                    if existing_lock.last_heartbeat > stale_threshold:
                        self.stdout.write(
                            self.style.ERROR(
                                f'Sync loop already running for device {device_id} '
                                f'(PID: {existing_lock.process_id}, acquired at: {existing_lock.acquired_at})'
                            )
                        )
                        self.stdout.write(
                            self.style.ERROR('Only one sync loop can run at a time. Exiting...')
                        )
                        return False
                    else:
                        # Lock is stale, remove it
                        existing_lock.delete()
                
                # Create new lock
                SyncLock.objects.create(
                    lock_name=lock_name,
                    device_id=device_id,
                    process_id=process_id,
                    is_active=True
                )
                
            self.stdout.write(
                self.style.SUCCESS(f'Acquired sync lock (PID: {process_id})')
            )
            return True
            
        except IntegrityError:
            # Another process acquired the lock between our check and create
            self.stdout.write(
                self.style.ERROR(
                    f'Failed to acquire lock - another sync loop is already running for device {device_id}'
                )
            )
            return False
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error acquiring lock: {e}')
            )
            return False
    
    def release_lock(self, device_id):
        """Release sync lock on exit"""
        lock_name = f'sync_loop_{device_id}'
        process_id = os.getpid()
        
        try:
            deleted_count, _ = SyncLock.objects.filter(
                lock_name=lock_name,
                process_id=process_id
            ).delete()
            
            if deleted_count > 0:
                self.stdout.write(f'Released lock for device {device_id}')
            else:
                self.stdout.write(
                    self.style.WARNING(f'No lock found to release for PID {process_id}')
                )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error releasing lock: {e}')
            )
