import json
from decimal import Decimal
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from .models import Product, Order, Transaction, DeviceChange
from .serializers import (
    SyncPushRequestSerializer, SyncPushResponseSerializer,
    SyncPullResponseSerializer, ProductSerializer, OrderSerializer, TransactionSerializer
)


class SyncPushView(APIView):
    """
    POST /sync/push
    Handles device changes and applies them to server with conflict resolution
    """
    
    def post(self, request):
        serializer = SyncPushRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        device_id = data['device_id']
        last_acked_local_seq = data['last_acked_local_seq']
        changes = data['changes']
        
        conflicts = []
        acked_up_to_local_seq = last_acked_local_seq
        
        with transaction.atomic():
            for change in changes:
                conflict = self._apply_change(change)
                if conflict:
                    conflicts.append(conflict)
                else:
                    acked_up_to_local_seq = change['local_seq']
            
            # Get current device sequence
            latest_device_change = DeviceChange.objects.order_by('-local_seq').first()
            server_seq = latest_device_change.local_seq if latest_device_change else 0
        
        response_data = {
            'acked_up_to_local_seq': acked_up_to_local_seq,
            'conflicts': conflicts,
            'server_seq': server_seq
        }
        
        return Response(response_data)
    
    def _apply_change(self, change):
        """Apply a single change and return conflict if any"""
        model_name = change['model']
        object_uuid = change['object_uuid']
        op = change['op']
        payload = change['payload']
        local_seq = change['local_seq']
        
        model_class = {
            'product': Product,
            'order': Order,
            'transaction': Transaction
        }[model_name]
        
        if op == 'delete':
            return self._apply_delete(model_class, object_uuid, local_seq, payload)
        else:  # upsert
            return self._apply_upsert(model_class, model_name, object_uuid, local_seq, payload)
    
    def _apply_delete(self, model_class, object_uuid, local_seq, payload):
        """Apply delete operation"""
        try:
            obj = model_class.objects.get(uuid=object_uuid)
            obj.soft_delete()
            
            # Create server change record
            self._create_server_change(
                model_class._meta.model_name, 
                object_uuid, 
                'delete', 
                self._serialize_object(obj)
            )
            return None
        except model_class.DoesNotExist:
            # Object doesn't exist, no conflict
            return None
    
    def _apply_upsert(self, model_class, model_name, object_uuid, local_seq, payload):
        """Apply upsert operation with conflict resolution"""
        try:
            existing_obj = model_class.objects.get(uuid=object_uuid)
            return self._handle_update_conflict(existing_obj, model_name, payload, local_seq)
        except model_class.DoesNotExist:
            return self._create_new_object(model_class, model_name, object_uuid, payload)
    
    def _handle_update_conflict(self, existing_obj, model_name, payload, local_seq):
        """Handle update conflicts based on model-specific rules"""
        
        if model_name == 'product':
            # Product rule: server wins if version <= existing version OR existing updated_at newer
            incoming_version = payload.get('version', 1)
            incoming_updated_at = payload.get('updated_at')
            
            if incoming_version <= existing_obj.version or (
                incoming_updated_at and existing_obj.updated_at > timezone.datetime.fromisoformat(incoming_updated_at.replace('Z', '+00:00'))
            ):
                return {
                    'local_seq': local_seq,
                    'object_uuid': existing_obj.uuid,
                    'server_object': self._serialize_object(existing_obj),
                    'reason': 'Product conflict: server version is newer'
                }
        
        elif model_name == 'order':
            # Order rule: allow updates only while is_closed=false
            if existing_obj.is_closed:
                # Check if it's an idempotent re-send (identical payload)
                current_data = self._serialize_object(existing_obj)
                if not self._payloads_equivalent(current_data, payload):
                    return {
                        'local_seq': local_seq,
                        'object_uuid': existing_obj.uuid,
                        'server_object': current_data,
                        'reason': 'Order conflict: order is closed and payload differs'
                    }
        
        elif model_name == 'transaction':
            # Transaction rule: append-only, reject if payload differs but UUID exists
            current_data = self._serialize_object(existing_obj)
            if not self._payloads_equivalent(current_data, payload):
                return {
                    'local_seq': local_seq,
                    'object_uuid': existing_obj.uuid,
                    'server_object': current_data,
                    'reason': 'Transaction conflict: transaction exists with different data'
                }
            else:
                # Idempotent success - no need to update
                return None
        
        # No conflict, update the object
        self._update_object(existing_obj, model_name, payload)
        return None
    
    def _create_new_object(self, model_class, model_name, object_uuid, payload):
        """Create a new object"""
        try:
            # Remove computed fields that shouldn't be set directly
            clean_payload = payload.copy()
            clean_payload.pop('created_at', None)
            clean_payload.pop('updated_at', None)
            
            obj = model_class(uuid=object_uuid, **clean_payload)
            obj.full_clean()
            obj.save()
            
            self._create_server_change(model_name, object_uuid, 'upsert', self._serialize_object(obj))
            return None
        except Exception as e:
            # Return validation error as conflict
            return {
                'local_seq': payload.get('local_seq', 0),
                'object_uuid': object_uuid,
                'server_object': None,
                'reason': f'Validation error: {str(e)}'
            }
    
    def _update_object(self, obj, model_name, payload):
        """Update existing object"""
        # Update fields from payload (excluding auto fields)
        exclude_fields = {'uuid', 'created_at', 'updated_at', 'deleted_at'}
        
        for field, value in payload.items():
            if field not in exclude_fields and hasattr(obj, field):
                # Handle Decimal fields for price/money values
                if field in ['price', 'total_tax', 'total_discount'] and isinstance(value, (int, float, str)):
                    value = Decimal(str(value))
                setattr(obj, field, value)
        
        obj.version += 1
        obj.save()
        
        self._create_server_change(model_name, obj.uuid, 'upsert', self._serialize_object(obj))
    
    def _serialize_object(self, obj):
        """Serialize object to JSON payload"""
        if isinstance(obj, Product):
            return ProductSerializer(obj).data
        elif isinstance(obj, Order):
            return OrderSerializer(obj).data
        elif isinstance(obj, Transaction):
            return TransactionSerializer(obj).data
    
    def _create_device_change(self, model, object_uuid, op, payload):
        """Create DeviceChange record"""
        DeviceChange.objects.create(
            device_id='LOCAL',
            model=model,
            object_uuid=object_uuid,
            op=op,
            payload=payload
        )
    
    def _payloads_equivalent(self, payload1, payload2):
        """Check if two payloads are equivalent (ignoring auto fields)"""
        exclude_fields = {'created_at', 'updated_at', 'version'}
        
        clean1 = {k: v for k, v in payload1.items() if k not in exclude_fields}
        clean2 = {k: v for k, v in payload2.items() if k not in exclude_fields}
        
        return clean1 == clean2


class SyncPullView(APIView):
    """
    GET /sync/pull?since=<server_seq>&device_id=<id>&limit=500
    Returns server changes since the given sequence
    """
    
    def get(self, request):
        since_seq = int(request.query_params.get('since', 0))
        device_id = request.query_params.get('device_id', '')
        limit = int(request.query_params.get('limit', 500))
        
        # Get changes since the given sequence
        changes = DeviceChange.objects.filter(local_seq__gt=since_seq).order_by('local_seq')[:limit]
        
        # Get latest sequence number
        latest_change = DeviceChange.objects.order_by('-local_seq').first()
        latest_server_seq = latest_change.local_seq if latest_change else 0
        
        response_data = {
            'latest_server_seq': latest_server_seq,
            'changes': [
                {
                    'seq': change.local_seq,
                    'model': change.model,
                    'object_uuid': change.object_uuid,
                    'op': change.op,
                    'payload': change.payload
                }
                for change in changes
            ]
        }
        
        return Response(response_data)
