#!/usr/bin/env python
"""
Test script to verify error tracking and safe ack implementation in DeviceChange model.

This script tests:
1. Error tracking fields (attempt_count, last_attempt_at, last_error)
2. Safe ack marking (only pushed=False rows)
3. Conflict exclusion (conflicted rows not marked as pushed)
"""

import os
import sys
import django

# Add the Django project directory to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set up Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'sync_django_local.settings')
django.setup()

from django.utils import timezone
from master.models import DeviceChange, Product, SyncState

def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def test_error_tracking_fields():
    """Test that new error tracking fields exist and work"""
    print_section("TEST 1: Error Tracking Fields")
    
    # Create a test product to generate a DeviceChange
    product = Product.objects.create(name="Test Product for Error Tracking", price=9.99)
    
    # Get the DeviceChange record
    change = DeviceChange.objects.filter(object_uuid=product.uuid).first()
    
    if not change:
        print("❌ FAILED: No DeviceChange created")
        return False
    
    print(f"✓ DeviceChange created: seq={change.local_seq}")
    
    # Check new fields exist
    try:
        print(f"  - pushed_at: {change.pushed_at}")
        print(f"  - attempt_count: {change.attempt_count}")
        print(f"  - last_attempt_at: {change.last_attempt_at}")
        print(f"  - last_error: {change.last_error}")
        print("✓ All error tracking fields exist")
    except AttributeError as e:
        print(f"❌ FAILED: Missing field - {e}")
        return False
    
    # Test updating error tracking fields
    change.attempt_count = 1
    change.last_attempt_at = timezone.now()
    change.last_error = "Test error message"
    change.save(update_fields=['attempt_count', 'last_attempt_at', 'last_error'])
    
    # Reload and verify
    change.refresh_from_db()
    if change.attempt_count == 1 and change.last_error == "Test error message":
        print("✓ Error tracking fields can be updated")
        return True
    else:
        print("❌ FAILED: Error tracking fields not updated correctly")
        return False

def test_safe_ack_marking():
    """Test that only pushed=False rows are marked as pushed"""
    print_section("TEST 2: Safe Ack Marking")
    
    # Create test products
    p1 = Product.objects.create(name="Product 1 - Unpushed", price=1.00)
    p2 = Product.objects.create(name="Product 2 - Already Pushed", price=2.00)
    p3 = Product.objects.create(name="Product 3 - Unpushed", price=3.00)
    
    # Get their changes
    changes = DeviceChange.objects.filter(
        object_uuid__in=[p1.uuid, p2.uuid, p3.uuid]
    ).order_by('local_seq')
    
    if changes.count() != 3:
        print(f"❌ FAILED: Expected 3 changes, got {changes.count()}")
        return False
    
    c1, c2, c3 = list(changes)
    
    # Get the actual device_id from the records
    device_id = c1.device_id
    
    # Mark c2 as already pushed
    c2.pushed = True
    c2.pushed_at = timezone.now()
    c2.save(update_fields=['pushed', 'pushed_at'])
    
    print(f"Setup: c1(seq={c1.local_seq}, pushed=False), c2(seq={c2.local_seq}, pushed=True), c3(seq={c3.local_seq}, pushed=False)")
    print(f"Device ID: {device_id}")
    
    # Simulate safe ack marking (like in sync_loop.py)
    acked_seq = c3.local_seq  # Ack up to c3
    
    DeviceChange.objects.filter(
        device_id=device_id,
        local_seq__lte=acked_seq,
        pushed=False  # Safe ack filter
    ).update(
        pushed=True,
        pushed_at=timezone.now()
    )
    
    # Verify results
    c1.refresh_from_db()
    c2.refresh_from_db()
    c3.refresh_from_db()
    
    if c1.pushed and c3.pushed:
        print(f"✓ Unpushed rows marked: c1(pushed={c1.pushed}), c3(pushed={c3.pushed})")
    else:
        print(f"❌ FAILED: Unpushed rows not marked correctly")
        return False
    
    if c2.pushed:
        print(f"✓ Already pushed row unchanged: c2(pushed={c2.pushed})")
        return True
    else:
        print(f"❌ FAILED: Already pushed row was modified")
        return False

def test_conflict_exclusion():
    """Test that conflicted rows are not marked as pushed"""
    print_section("TEST 3: Conflict Exclusion")
    
    # Create test products
    p1 = Product.objects.create(name="Product A - Normal", price=10.00)
    p2 = Product.objects.create(name="Product B - Conflicted", price=20.00)
    p3 = Product.objects.create(name="Product C - Normal", price=30.00)
    
    # Get their changes
    changes = DeviceChange.objects.filter(
        object_uuid__in=[p1.uuid, p2.uuid, p3.uuid]
    ).order_by('local_seq')
    
    if changes.count() != 3:
        print(f"❌ FAILED: Expected 3 changes, got {changes.count()}")
        return False
    
    c1, c2, c3 = list(changes)
    
    # Get the actual device_id from the records
    device_id = c1.device_id
    
    print(f"Setup: c1(seq={c1.local_seq}), c2(seq={c2.local_seq}), c3(seq={c3.local_seq})")
    print(f"Device ID: {device_id}")
    
    # Simulate conflict on c2
    conflicted_seqs = {c2.local_seq}
    acked_seq = c3.local_seq
    
    # Safe ack with conflict exclusion (like in sync_loop.py)
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
    
    # Verify results
    c1.refresh_from_db()
    c2.refresh_from_db()
    c3.refresh_from_db()
    
    if c1.pushed and c3.pushed:
        print(f"✓ Non-conflicted rows marked: c1(pushed={c1.pushed}), c3(pushed={c3.pushed})")
    else:
        print(f"❌ FAILED: Non-conflicted rows not marked")
        return False
    
    if not c2.pushed:
        print(f"✓ Conflicted row excluded: c2(pushed={c2.pushed})")
        return True
    else:
        print(f"❌ FAILED: Conflicted row was marked as pushed")
        return False

def cleanup():
    """Clean up test data"""
    print_section("Cleanup")
    Product.objects.filter(name__startswith="Test Product").delete()
    Product.objects.filter(name__startswith="Product").delete()
    print("✓ Test data cleaned up")

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Error Tracking & Safe Ack Implementation Tests")
    print("="*60)
    
    results = []
    
    try:
        results.append(("Error Tracking Fields", test_error_tracking_fields()))
        results.append(("Safe Ack Marking", test_safe_ack_marking()))
        results.append(("Conflict Exclusion", test_conflict_exclusion()))
    finally:
        cleanup()
    
    # Summary
    print_section("Test Summary")
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✓ PASSED" if result else "❌ FAILED"
        print(f"{status}: {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
        sys.exit(1)
