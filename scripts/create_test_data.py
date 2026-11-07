#!/usr/bin/env python
import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sync_django_local.settings')
django.setup()

from master.models import Product, Order, Transaction, DeviceChange

def create_test_data():
    print('Creating test products on local POS...')
    
    # Create test products on local POS
    p1 = Product.objects.create(name='Local Coffee', price=4.50)
    p2 = Product.objects.create(name='Local Sandwich', price=8.99)
    p3 = Product.objects.create(name='Local Pastry', price=3.25)

    print(f'Created products:')
    print(f'- {p1.name}: ${p1.price} (UUID: {p1.uuid})')
    print(f'- {p2.name}: ${p2.price} (UUID: {p2.uuid})')
    print(f'- {p3.name}: ${p3.price} (UUID: {p3.uuid})')

    # Create a test order
    order = Order.objects.create(
        order_number='ORD-001',
        store_id=1,
        state='cart',
        is_closed=False,
        total_tax=1.20,
        total_discount=0.00
    )
    print(f'Created order: {order.order_number} (UUID: {order.uuid})')

    # Create a test transaction
    transaction = Transaction.objects.create(
        transaction_number='TXN-001',
        transaction_type='payment',
        order_uuid=order.uuid
    )
    print(f'Created transaction: {transaction.transaction_number} (UUID: {transaction.uuid})')

    # Check DeviceChange records
    changes = DeviceChange.objects.filter(pushed=False)
    print(f'\nUnpushed changes: {changes.count()}')
    for change in changes:
        print(f'- {change.model} {change.op} for {change.object_uuid}')

if __name__ == '__main__':
    create_test_data()
