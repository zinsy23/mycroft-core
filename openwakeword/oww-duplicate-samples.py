#!/usr/bin/env python3
"""
Duplicate audio samples for OpenWakeWord training data balancing.

This script duplicates specified audio samples with auto-incrementing numbers,
continuing from existing duplicates if they already exist.

Usage:
    python oww-duplicate-samples.py \\
        --sample "path/to/file1.wav" --count 15 \\
        --sample "path/to/file2.wav" --count 80

The script will:
1. Check for existing duplicates (e.g., file1_dup_01.wav, file1_dup_02.wav)
2. Find the highest number already used
3. Create additional duplicates on top of existing ones up to the specified count
"""

import argparse
import os
import shutil
import sys
from pathlib import Path


def find_existing_duplicates(directory, base_name, extension):
    """
    Find existing duplicate files and return the highest number used.

    Args:
        directory: Directory to search in
        base_name: Base filename without extension (e.g., "talking_sample")
        extension: File extension (e.g., ".wav")

    Returns:
        Highest duplicate number found, or 0 if none exist
    """
    pattern = f"{base_name}_dup_"
    highest = 0

    for filename in os.listdir(directory):
        if filename.startswith(pattern) and filename.endswith(extension):
            # Extract number from filename like "talking_sample_dup_05.wav"
            try:
                num_str = filename[len(pattern):-len(extension)]
                num = int(num_str)
                highest = max(highest, num)
            except ValueError:
                continue

    return highest


def duplicate_sample(sample_path, count):
    """
    Duplicate a sample file the specified number of times.

    Args:
        sample_path: Path to the sample file to duplicate
        count: Number of additional duplicates to create on top of existing ones

    Returns:
        Number of new duplicates created
    """
    sample_path = Path(sample_path).expanduser().resolve()

    if not sample_path.exists():
        print(f"  ✗ Error: File not found: {sample_path}")
        return 0

    directory = sample_path.parent
    base_name = sample_path.stem
    extension = sample_path.suffix

    # Find existing duplicates
    existing_count = find_existing_duplicates(directory, base_name, extension)

    # Add count on top of existing
    start_num = existing_count + 1
    end_num = existing_count + count
    new_total = end_num

    print(f"  Duplicating '{sample_path.name}' from #{start_num} to #{end_num} ({count} new duplicates, total: {new_total})...")

    for i in range(start_num, end_num + 1):
        # Determine width for current number (gracefully expand digits as needed)
        num_width = max(2, len(str(i)))
        duplicate_name = f"{base_name}_dup_{i:0{num_width}d}{extension}"
        duplicate_path = directory / duplicate_name
        shutil.copy2(sample_path, duplicate_path)

    return count


def main():
    parser = argparse.ArgumentParser(
        description="Duplicate audio samples for OpenWakeWord training data balancing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Duplicate single sample
  python oww-duplicate-samples.py \\
      --sample "/path/to/talking_sample.wav" --count 15

  # Duplicate multiple samples
  python oww-duplicate-samples.py \\
      --sample "/path/to/talking_sample.wav" --count 15 \\
      --sample "/path/to/false_positives.wav" --count 80 \\
      --sample "/path/to/com_words.wav" --count 80

The script creates files like: original_name_dup_01.wav, original_name_dup_02.wav, etc.
If duplicates already exist, it continues numbering from where it left off.
        """
    )

    parser.add_argument(
        '--sample',
        action='append',
        dest='samples',
        metavar='PATH',
        help='Path to sample file to duplicate (can be specified multiple times)'
    )

    parser.add_argument(
        '--count',
        action='append',
        dest='counts',
        type=int,
        metavar='N',
        help='Number of additional duplicates to create for the preceding --sample (adds on top of existing)'
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.samples or not args.counts:
        parser.print_help()
        sys.exit(1)

    if len(args.samples) != len(args.counts):
        print("Error: Each --sample must have a corresponding --count")
        print(f"  Got {len(args.samples)} samples and {len(args.counts)} counts")
        sys.exit(1)

    # Process each sample
    print("=" * 50)
    print("Duplicating Audio Samples")
    print("=" * 50)
    print()

    total_created = 0
    summary = []

    for sample_path, count in zip(args.samples, args.counts):
        existing = find_existing_duplicates(
            Path(sample_path).expanduser().resolve().parent,
            Path(sample_path).stem,
            Path(sample_path).suffix
        )
        created = duplicate_sample(sample_path, count)
        total_created += created

        sample_name = Path(sample_path).name
        new_total = existing + count
        summary.append(f"  - {sample_name}: +{count} new (total: {new_total} duplicates)")

    print()
    print("=" * 50)
    print(f"Complete! Created {total_created} new duplicates")
    print("=" * 50)
    print()
    print("Summary:")
    for line in summary:
        print(line)
    print()


if __name__ == "__main__":
    main()
