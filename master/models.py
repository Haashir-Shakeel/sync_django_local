import uuid
from django.db import models
from django.utils import timezone


class SyncBaseModel(models.Model):
    """Base model for all sync-enabled models"""
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        """Override save to increment version on updates"""
        # Don't increment version if this is a sync operation from master
        if not getattr(self, '_syncing_from_master', False):
            # Only increment version on updates, not creates
            if self.pk is not None:  # This is an update
                # Check if we're actually updating the object by fetching current version
                try:
                    current = self.__class__.objects.get(pk=self.pk)
                    if current.version == self.version:  # Version hasn't been manually set
                        self.version += 1
                except self.__class__.DoesNotExist:
                    pass  # Object doesn't exist yet, this is effectively a create
        
        super().save(*args, **kwargs)

    def soft_delete(self):
        """Soft delete the object by setting deleted_at"""
        self.deleted_at = timezone.now()
        self.version += 1
        self.save()

    def is_deleted(self):
        """Check if object is soft deleted"""
        return self.deleted_at is not None


class Product(SyncBaseModel):
    """Product model for sync - matches sync specification exactly"""
    name = models.CharField(max_length=100, blank=False, null=False)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.name} - ${self.price}"


class Order(SyncBaseModel):
    """Order model for sync - matches sync specification exactly"""
    CART_STATE_CHOICES = [
        ('cart', 'Cart'),
        ('order', 'Order'),
    ]
    
    order_number = models.CharField(max_length=50, null=True, blank=True)
    store_id = models.IntegerField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=CART_STATE_CHOICES, default='cart')
    is_closed = models.BooleanField(default=False)
    total_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return f"Order {self.order_number or self.uuid} ({self.state})"


class Transaction(SyncBaseModel):
    """Transaction model for sync - matches sync specification exactly"""
    TRANSACTION_TYPE_CHOICES = [
        ('payment', 'Payment'),
        ('refund', 'Refund'),
        ('void', 'Void'),
        ('adjustment', 'Adjustment'),
    ]

    transaction_number = models.CharField(max_length=50, null=True, blank=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    order_uuid = models.UUIDField(null=True, blank=True)  # nullable foreign key reference

    def __str__(self):
        return f"Transaction {self.transaction_number or self.uuid} ({self.transaction_type})"


class DeviceChange(models.Model):
    """DeviceChange model for tracking local changes that need to be synced to master"""
    OPERATION_CHOICES = [
        ('upsert', 'Upsert'),
        ('delete', 'Delete'),
    ]
    
    MODEL_CHOICES = [
        ('product', 'Product'),
        ('order', 'Order'),
        ('transaction', 'Transaction'),
    ]

    device_id = models.CharField(max_length=50)
    local_seq = models.AutoField(primary_key=True)  # auto-incrementing sequence
    model = models.CharField(max_length=20, choices=MODEL_CHOICES)
    object_uuid = models.UUIDField()
    op = models.CharField(max_length=10, choices=OPERATION_CHOICES)
    payload = models.JSONField()  # JSON payload of the object
    ts = models.DateTimeField(auto_now_add=True)  # timestamp
    pushed = models.BooleanField(default=False)  # whether this change has been pushed to master
    pushed_at = models.DateTimeField(null=True, blank=True)  # when successfully pushed

    attempt_count = models.PositiveIntegerField(default=0)  # number of push attempts
    last_attempt_at = models.DateTimeField(null=True, blank=True)  # last push attempt timestamp
    last_error = models.TextField(null=True, blank=True)  # last error message

    class Meta:
        ordering = ['local_seq']  # ensure consistent ordering by sequence

    def __str__(self):
        return f"DeviceChange {self.local_seq}: {self.op} {self.model} {self.object_uuid} (pushed: {self.pushed})"


class SyncState(models.Model):
    """SyncState model for tracking sync progress for this device"""
    device_id = models.CharField(max_length=50, primary_key=True)
    last_acked_local_seq = models.PositiveIntegerField(default=0)  # last local sequence acknowledged by master
    last_server_seq = models.PositiveIntegerField(default=0)  # last server sequence we've processed
    last_sync_ts = models.DateTimeField(null=True, blank=True)  # last successful sync timestamp

    def __str__(self):
        return f"SyncState {self.device_id}: local_seq={self.last_acked_local_seq}, server_seq={self.last_server_seq}"


class LocalConflict(models.Model):
    """LocalConflict model for storing conflicts returned by master during sync"""
    device_id = models.CharField(max_length=50)
    local_seq = models.PositiveIntegerField()  # the local sequence that caused the conflict
    model = models.CharField(max_length=20)
    object_uuid = models.UUIDField()
    conflict_data = models.JSONField()  # conflict details from master
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['device_id', 'local_seq']

    def __str__(self):
        return f"LocalConflict {self.device_id}:{self.local_seq} {self.model} {self.object_uuid} (resolved: {self.resolved})"


class LocalDeviceConfig(models.Model):
    """LocalDeviceConfig model for storing device authentication and sync configuration"""
    device_id = models.CharField(max_length=50, unique=True, primary_key=True)
    sync_token = models.CharField(max_length=255)
    master_url = models.URLField()
    business_uuid = models.UUIDField()
    store_uuid = models.UUIDField(null=True, blank=True)
    sync_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"LocalDeviceConfig {self.device_id} (sync_enabled: {self.sync_enabled})"


class SyncLock(models.Model):
    """SyncLock model to ensure only one sync loop runs at a time (singleton pattern)"""
    lock_name = models.CharField(max_length=50, primary_key=True)
    device_id = models.CharField(max_length=50)
    process_id = models.IntegerField()
    acquired_at = models.DateTimeField(auto_now_add=True)
    last_heartbeat = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"SyncLock {self.lock_name} (device: {self.device_id}, pid: {self.process_id}, active: {self.is_active})"
