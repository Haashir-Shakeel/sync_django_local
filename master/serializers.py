from rest_framework import serializers
from .models import Product, Order, Transaction, DeviceChange


class ProductSerializer(serializers.ModelSerializer):
    """Serializer for Product model"""
    class Meta:
        model = Product
        fields = ['uuid', 'name', 'price', 'created_at', 'updated_at', 'deleted_at', 'version']


class OrderSerializer(serializers.ModelSerializer):
    """Serializer for Order model"""
    class Meta:
        model = Order
        fields = ['uuid', 'order_number', 'store_id', 'state', 'is_closed', 
                 'total_tax', 'total_discount', 'created_at', 'updated_at', 'deleted_at', 'version']


class TransactionSerializer(serializers.ModelSerializer):
    """Serializer for Transaction model"""
    class Meta:
        model = Transaction
        fields = ['uuid', 'transaction_number', 'transaction_type', 'order_uuid', 
                 'created_at', 'updated_at', 'deleted_at', 'version']


class DeviceChangeSerializer(serializers.ModelSerializer):
    """Serializer for DeviceChange model"""
    class Meta:
        model = DeviceChange
        fields = ['local_seq', 'device_id', 'model', 'object_uuid', 'op', 'payload', 'ts', 'pushed']


# Sync API specific serializers
class SyncChangeSerializer(serializers.Serializer):
    """Serializer for individual change in sync push request"""
    local_seq = serializers.IntegerField()
    model = serializers.ChoiceField(choices=['product', 'order', 'transaction'])
    object_uuid = serializers.UUIDField()
    op = serializers.ChoiceField(choices=['upsert', 'delete'])
    payload = serializers.JSONField()


class SyncPushRequestSerializer(serializers.Serializer):
    """Serializer for sync push request"""
    device_id = serializers.CharField(max_length=100)
    last_acked_local_seq = serializers.IntegerField()
    changes = SyncChangeSerializer(many=True)


class ConflictSerializer(serializers.Serializer):
    """Serializer for conflict response"""
    local_seq = serializers.IntegerField()
    object_uuid = serializers.UUIDField()
    server_object = serializers.JSONField()
    reason = serializers.CharField()


class SyncPushResponseSerializer(serializers.Serializer):
    """Serializer for sync push response"""
    acked_up_to_local_seq = serializers.IntegerField()
    conflicts = ConflictSerializer(many=True)
    server_seq = serializers.IntegerField()


class SyncPullResponseSerializer(serializers.Serializer):
    """Serializer for sync pull response"""
    latest_server_seq = serializers.IntegerField()
    changes = DeviceChangeSerializer(many=True)
