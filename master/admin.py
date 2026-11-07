from django.contrib import admin
from .models import Product, Order, Transaction, DeviceChange, SyncState, LocalConflict, LocalDeviceConfig, SyncLock

# Register your models here.
admin.site.site_header = "POS Local Admin"

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['uuid', 'name', 'price', 'version', 'created_at', 'updated_at', 'deleted_at']
    list_filter = ['created_at', 'updated_at', 'deleted_at']
    search_fields = ['name', 'uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    ordering = ['-created_at']

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['uuid', 'order_number', 'state', 'is_closed', 'store_id', 'total_tax', 'total_discount', 'version', 'created_at']
    list_filter = ['state', 'is_closed', 'created_at', 'deleted_at']
    search_fields = ['order_number', 'uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    ordering = ['-created_at']

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['uuid', 'transaction_number', 'transaction_type', 'order_uuid', 'version', 'created_at']
    list_filter = ['transaction_type', 'created_at', 'deleted_at']
    search_fields = ['transaction_number', 'uuid', 'order_uuid']
    readonly_fields = ['uuid', 'created_at', 'updated_at']
    ordering = ['-created_at']

@admin.register(DeviceChange)
class DeviceChangeAdmin(admin.ModelAdmin):
    list_display = ['local_seq', 'device_id', 'model', 'object_uuid', 'op', 'pushed', 'ts']
    list_filter = ['device_id', 'model', 'op', 'pushed', 'ts']
    search_fields = ['object_uuid', 'device_id']
    readonly_fields = ['local_seq', 'ts']
    ordering = ['-local_seq']

    def has_add_permission(self, request):
        # Prevent manual creation of DeviceChange records
        return False

    def has_change_permission(self, request, obj=None):
        # Allow editing only the pushed field
        return True

@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ['device_id', 'last_acked_local_seq', 'last_server_seq', 'last_sync_ts']
    readonly_fields = ['device_id']
    ordering = ['device_id']

@admin.register(LocalConflict)
class LocalConflictAdmin(admin.ModelAdmin):
    list_display = ['device_id', 'local_seq', 'model', 'object_uuid', 'resolved', 'created_at']
    list_filter = ['device_id', 'model', 'resolved', 'created_at']
    search_fields = ['object_uuid', 'device_id']
    readonly_fields = ['device_id', 'local_seq', 'model', 'object_uuid', 'conflict_data', 'created_at']
    ordering = ['-created_at']

@admin.register(LocalDeviceConfig)
class LocalDeviceConfigAdmin(admin.ModelAdmin):
    list_display = ['device_id', 'master_url', 'business_uuid', 'store_uuid', 'sync_enabled', 'created_at']
    list_filter = ['sync_enabled', 'created_at']
    search_fields = ['device_id', 'business_uuid', 'store_uuid']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['device_id']

@admin.register(SyncLock)
class SyncLockAdmin(admin.ModelAdmin):
    list_display = ['lock_name', 'device_id', 'process_id', 'acquired_at', 'last_heartbeat', 'is_active']
    list_filter = ['device_id', 'is_active', 'acquired_at']
    search_fields = ['lock_name', 'device_id']
    readonly_fields = ['lock_name', 'device_id', 'process_id', 'acquired_at', 'last_heartbeat']
    ordering = ['-acquired_at']
    
    def has_add_permission(self, request):
        # Prevent manual creation of SyncLock records
        return False
