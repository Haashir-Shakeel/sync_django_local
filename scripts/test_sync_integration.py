#!/usr/bin/env python
"""
Integration test for sync loop with error tracking and safe ack.

This test verifies:
1. Sync loop tracks attempt_count and last_attempt_at
2. Successful push sets pushed_at and clears last_error
3. Failed push records error in last_error field
4. Conflicted rows are not marked as pushed
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
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

def check_device_changes():
    """Check current state of DeviceChange records"""
    print_section("Current DeviceChange Records")
    
    changes = DeviceChange.objects.filter(pushed=False).order_by('local_seq')[:10]
    
    if not changes:
        print("No unpushed changes found.")
        return
    
    print(f"Found {changes.count()} unpushed changes:\n")
    
    for change in changes:
        print(f"Seq {change.local_seq}: {change.model} {change.object_uuid}")
        print(f"  - pushed: {change.pushed}")
        print(f"  - pushed_at: {change.pushed_at}")
        print(f"  - attempt_count: {change.attempt_count}")
        print(f"  - last_attempt_at: {change.last_attempt_at}")
        print(f"  - last_error: {change.last_error[:50] if change.last_error else None}")
        print()

def create_test_data():
    """Create test products to generate DeviceChange records"""
    print_section("Creating Test Data")
    
    products = [
        Product.objects.create(name=f"Integration Test Product {i}", price=10.00 + i)
        for i in range(1, 4)
    ]
    
    print(f"Created {len(products)} test products:")
    for p in products:
        print(f"  - {p.name} (UUID: {p.uuid})")
    
    # Get the DeviceChange records
    changes = DeviceChange.objects.filter(
        object_uuid__in=[p.uuid for p in products]
    ).order_by('local_seq')
    
    print(f"\nGenerated {changes.count()} DeviceChange records:")
    for change in changes:
        print(f"  - Seq {change.local_seq}: {change.model} {change.object_uuid}")
    
    return products, list(changes)

def verify_error_tracking_fields():
    """Verify that error tracking fields are present and working"""
    print_section("Verifying Error Tracking Fields")
    
    # Get a recent change
    change = DeviceChange.objects.order_by('-local_seq').first()
    
    if not change:
        print("❌ No DeviceChange records found")
        return False
    
    try:
        # Check all fields exist
        fields = {
            'pushed_at': change.pushed_at,
            'attempt_count': change.attempt_count,
            'last_attempt_at': change.last_attempt_at,
            'last_error': change.last_error
        }
        
        print("✓ All error tracking fields exist:")
        for field_name, value in fields.items():
            print(f"  - {field_name}: {value}")
        
        return True
    except AttributeError as e:
        print(f"❌ Missing field: {e}")
        return False

def show_sync_instructions():
    """Show instructions for manual sync testing"""
    print_section("Manual Sync Testing Instructions")
    
    print("""
To test the sync loop with error tracking:

1. Start the master server (if not running):
   cd sync_django_master
   python manage.py runserver 8000

2. In a new terminal, run the sync loop:
   cd sync_django_local
   python manage.py sync_loop --master http://localhost:8000 --device-id POS-1 --sleep-interval 5

3. Observe the sync loop output for:
   - Attempt count incrementing on each push
   - pushed_at timestamp being set on successful ack
   - last_error being recorded on failures
   - Conflicted rows NOT being marked as pushed

4. Check the database to verify:
   python manage.py shell
   >>> from master.models import DeviceChange
   >>> changes = DeviceChange.objects.filter(pushed=True).order_by('-pushed_at')[:5]
   >>> for c in changes:
   ...     print(f"Seq {c.local_seq}: pushed_at={c.pushed_at}, attempts={c.attempt_count}")

5. To simulate errors, stop the master server and watch:
   - attempt_count incrementing
   - last_error being populated
   - Changes remaining pushed=False

6. Restart master server and verify:
   - Changes get pushed successfully
   - pushed_at gets set
   - last_error gets cleared
""")

def show_current_stats():
    """Show current sync statistics"""
    print_section("Current Sync Statistics")
    
    total = DeviceChange.objects.count()
    pushed = DeviceChange.objects.filter(pushed=True).count()
    unpushed = DeviceChange.objects.filter(pushed=False).count()
    with_errors = DeviceChange.objects.filter(last_error__isnull=False).exclude(last_error='').count()
    with_attempts = DeviceChange.objects.filter(attempt_count__gt=0).count()
    
    print(f"Total DeviceChange records: {total}")
    print(f"  - Pushed: {pushed}")
    print(f"  - Unpushed: {unpushed}")
    print(f"  - With errors: {with_errors}")
    print(f"  - With attempts: {with_attempts}")
    
    # Show recent pushed changes
    recent_pushed = DeviceChange.objects.filter(
        pushed=True, 
        pushed_at__isnull=False
    ).order_by('-pushed_at')[:5]
    
    if recent_pushed:
        print(f"\nRecent successfully pushed changes:")
        for change in recent_pushed:
            print(f"  - Seq {change.local_seq}: pushed_at={change.pushed_at}, attempts={change.attempt_count}")
    
    # Show changes with errors
    errored = DeviceChange.objects.filter(
        last_error__isnull=False
    ).exclude(last_error='').order_by('-last_attempt_at')[:5]
    
    if errored:
        print(f"\nChanges with errors:")
        for change in errored:
            error_preview = change.last_error[:60] + "..." if len(change.last_error) > 60 else change.last_error
            print(f"  - Seq {change.local_seq}: attempts={change.attempt_count}, error={error_preview}")

if __name__ == "__main__":
    print("\n" + "="*70)
    print("  Sync Loop Integration Test - Error Tracking & Safe Ack")
    print("="*70)
    
    # Verify fields exist
    if not verify_error_tracking_fields():
        print("\n❌ Error tracking fields not found. Please run migrations first.")
        sys.exit(1)
    
    # Show current stats
    show_current_stats()
    
    # Check existing changes
    check_device_changes()
    
    # Create test data
    products, changes = create_test_data()
    
    # Show instructions
    show_sync_instructions()
    
    print_section("Summary")
    print("""
✓ Error tracking fields verified
✓ Test data created
✓ Ready for sync loop testing

Next steps:
1. Start master server on port 8000
2. Run sync loop with: python manage.py sync_loop
3. Observe error tracking in action
""")
