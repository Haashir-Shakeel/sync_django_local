import json
from decimal import Decimal
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.conf import settings
from django.forms.models import model_to_dict
from django.utils import timezone
from .models import Product, Order, Transaction, DeviceChange, SyncState


def get_model_name(instance):
    """Get the model name for DeviceChange tracking"""
    model_map = {
        Product: 'product',
        Order: 'order', 
        Transaction: 'transaction'
    }
    return model_map.get(type(instance))


def serialize_instance(instance):
    """Serialize model instance to JSON payload"""
    # Manually build the data dictionary to avoid model_to_dict issues
    data = {}
    
    # Add UUID field first
    data['uuid'] = str(instance.uuid)
    
    # Add all other fields manually
    for field in instance._meta.fields:
        field_name = field.name
        if field_name == 'uuid':
            continue  # Already handled above
            
        field_value = getattr(instance, field_name)
        
        # Handle datetime fields
        if field_name in ['created_at', 'updated_at', 'deleted_at']:
            if field_value:
                data[field_name] = field_value.isoformat()
            else:
                data[field_name] = None
        # Handle decimal fields - serialize as string to avoid float precision loss
        elif isinstance(field_value, Decimal):
            data[field_name] = str(field_value)
        # Handle UUID fields (like order_uuid)
        elif field_value is not None and hasattr(field_value, 'hex'):
            data[field_name] = str(field_value)
        # Handle other fields
        else:
            data[field_name] = field_value
    
    return data


def create_device_change(instance, operation):
    """Create a DeviceChange record for sync tracking"""
    device_id = getattr(settings, 'DEVICE_ID', 'POS-1')
    model_name = get_model_name(instance)
    
    if not model_name:
        return  # Skip if not a tracked model
    
    # Ensure SyncState exists for this device
    sync_state, created = SyncState.objects.get_or_create(
        device_id=device_id,
        defaults={
            'last_acked_local_seq': 0,
            'last_server_seq': 0,
            'last_sync_ts': None
        }
    )
    
    # Create the DeviceChange record
    DeviceChange.objects.create(
        device_id=device_id,
        model=model_name,
        object_uuid=instance.uuid,
        op=operation,
        payload=serialize_instance(instance),
        pushed=False
    )


@receiver(post_save, sender=Product)
@receiver(post_save, sender=Order)
@receiver(post_save, sender=Transaction)
def handle_model_save(sender, instance, created, **kwargs):
    """Handle create/update operations on sync models
    
    Note: Soft deletes are also tracked here as upserts with deleted_at set.
    We do NOT use post_delete signal - all deletes must be soft deletes.
    """
    # Only log changes if this is not a sync operation from master
    # We can detect this by checking if we're in a sync context
    if getattr(instance, '_syncing_from_master', False):
        return
    
    # Determine operation type based on deleted_at field
    # If deleted_at is set, this is a delete operation, otherwise it's an upsert
    # Test verified on master with op = 'delete' for soft deletes
    if instance.deleted_at is not None:
        create_device_change(instance, 'delete')
    else:
        create_device_change(instance, 'upsert')
