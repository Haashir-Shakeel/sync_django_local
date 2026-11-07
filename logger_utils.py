"""
Utility for redirecting stdout and stderr to log files
"""
import sys
import os
from datetime import datetime
from contextlib import contextmanager


class TeeOutput:
    """A class that writes to both file and original stream"""
    def __init__(self, file_path, original_stream):
        self.file = open(file_path, 'a', encoding='utf-8')
        self.original = original_stream

    def write(self, data):
        # Write to both file and original stream
        self.file.write(data)
        self.file.flush()  # Ensure immediate write
        self.original.write(data)
        self.original.flush()

    def flush(self):
        self.file.flush()
        self.original.flush()

    def close(self):
        self.file.close()


def setup_logging(log_dir="logs"):
    """
    Setup stdout and stderr logging to files
    """
    # Create logs directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)

    # Generate timestamp for log files
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Create log file paths
    stdout_log = os.path.join(log_dir, f"stdout_{timestamp}.log")
    stderr_log = os.path.join(log_dir, f"stderr_{timestamp}.log")

    # Also create a combined log file
    combined_log = os.path.join(log_dir, f"combined_{timestamp}.log")

    print("Setting up logging:")
    print(f"  - stdout -> {stdout_log}")
    print(f"  - stderr -> {stderr_log}")
    print(f"  - combined -> {combined_log}")

    # Redirect stdout and stderr
    sys.stdout = TeeOutput(stdout_log, sys.stdout)
    sys.stderr = TeeOutput(stderr_log, sys.stderr)

    # Also create a combined logger for both
    class CombinedLogger:
        def __init__(self, file_path):
            self.file = open(file_path, 'a', encoding='utf-8')

        def log(self, message, stream_type="INFO"):
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.file.write(f"[{timestamp}] [{stream_type}] {message}\n")
            self.file.flush()

        def close(self):
            self.file.close()

    combined_logger = CombinedLogger(combined_log)

    return {
        'stdout_log': stdout_log,
        'stderr_log': stderr_log,
        'combined_log': combined_log,
        'combined_logger': combined_logger
    }


def log_exception(exc_info, logger=None):
    """
    Log exception details to stderr and optionally to a combined logger
    """
    import traceback

    exc_type, exc_value, exc_traceback = exc_info
    error_msg = f"Exception occurred: {exc_type.__name__}: {exc_value}"
    traceback_msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))

    # Print to stderr (which will be logged)
    print(f"ERROR: {error_msg}", file=sys.stderr)
    print(f"Traceback:\n{traceback_msg}", file=sys.stderr)

    # Also log to combined logger if provided
    if logger:
        logger.log(error_msg, "ERROR")
        logger.log(f"Traceback:\n{traceback_msg}", "ERROR")


@contextmanager
def temporary_logging(log_dir="logs"):
    """
    Context manager for temporary logging setup
    """
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    try:
        log_info = setup_logging(log_dir)
        yield log_info
    finally:
        # Restore original streams
        if hasattr(sys.stdout, 'close'):
            sys.stdout.close()
        if hasattr(sys.stderr, 'close'):
            sys.stderr.close()

        sys.stdout = original_stdout
        sys.stderr = original_stderr