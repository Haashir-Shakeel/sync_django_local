#!/usr/bin/env python
import os
import django
import sys

# Add the Django project directory to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set up Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sync_django_local.settings')
django.setup()

from master.models import Product, Order, Transaction, DeviceChange, LocalConflict
from decimal import Decimal

print("=== TESTING VERSION INCREMENT ISSUE ON OTHER MODELS ===")

# Test with Order model
try:
    order = Order.objects.create(order_number="TEST001", store_id=1, state="cart")
    print(f"\nCreated Order: {order.uuid}")
    print(f"Initial version: {order.version}")
    
    # Update order
    order.total_tax = Decimal('10.50')
    order.save()
    order.refresh_from_db()
    
    print(f"After update - version: {order.version} (should be 2 if incrementing)")
    
    # Check DeviceChanges
    changes = DeviceChange.objects.filter(object_uuid=order.uuid).count()
    print(f"DeviceChanges created: {changes}")
    
except Exception as e:
    print(f"Order test failed: {e}")

# Test with Transaction model
try:
    transaction = Transaction.objects.create(
        transaction_number="TXN001", 
        transaction_type="payment"
    )
    print(f"\nCreated Transaction: {transaction.uuid}")
    print(f"Initial version: {transaction.version}")
    
    # Update transaction
    transaction.transaction_number = "TXN001-UPDATED"
    transaction.save()
    transaction.refresh_from_db()
    
    print(f"After update - version: {transaction.version} (should be 2 if incrementing)")
    
    # Check DeviceChanges
    changes = DeviceChange.objects.filter(object_uuid=transaction.uuid).count()
    print(f"DeviceChanges created: {changes}")
    
except Exception as e:
    print(f"Transaction test failed: {e}")

print("\n=== CONCLUSION ===")
print("If all models show version=1 after updates, the issue affects all models")
print("The local device needs to increment version on save()")
