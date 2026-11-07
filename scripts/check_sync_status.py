#!/usr/bin/env python
import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sync_django_local.settings')
django.setup()

from master.models import Product, Order, Transaction, DeviceChange, SyncState, LocalConflict

def check_sync_status():
    print("=== LOCAL POS SYNC STATUS ===\n")
    
    # Check sync state
    sync_states = SyncState.objects.all()
    print("Sync States:")
    for state in sync_states:
        print(f"- Device: {state.device_id}")
        print(f"  Last acked local seq: {state.last_acked_local_seq}")
        print(f"  Last server seq: {state.last_server_seq}")
        print(f"  Last sync: {state.last_sync_ts}")
    
    # Check local data
    print(f"\nLocal Data:")
    print(f"- Products: {Product.objects.count()}")
    print(f"- Orders: {Order.objects.count()}")
    print(f"- Transactions: {Transaction.objects.count()}")
    
    # Check device changes
    total_changes = DeviceChange.objects.count()
    pushed_changes = DeviceChange.objects.filter(pushed=True).count()
    unpushed_changes = DeviceChange.objects.filter(pushed=False).count()
    
    print(f"\nDevice Changes:")
    print(f"- Total: {total_changes}")
    print(f"- Pushed: {pushed_changes}")
    print(f"- Unpushed: {unpushed_changes}")
    
    # Check conflicts
    conflicts = LocalConflict.objects.all()
    print(f"\nConflicts: {conflicts.count()}")
    for conflict in conflicts:
        print(f"- Seq {conflict.local_seq}: {conflict.model} {conflict.object_uuid}")
        print(f"  Resolved: {conflict.resolved}")
    
    # Show some sample data
    print(f"\nSample Products:")
    for product in Product.objects.all()[:3]:
        print(f"- {product.name}: ${product.price} (v{product.version})")
    
    print(f"\nSample Orders:")
    for order in Order.objects.all()[:3]:
        print(f"- {order.order_number}: {order.state}, closed={order.is_closed}")

if __name__ == '__main__':
    check_sync_status()
