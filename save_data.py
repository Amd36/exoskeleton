# """
# save_data.py
# ------------
# Records EXACTLY N packets using DataLogger class and saves combined CSV.
# Uses the object-oriented approach with DataLogger instead of standalone functions.
# """

# import argparse
# from new_data_logger import DataLogger


# def main():
#     parser = argparse.ArgumentParser(description="Record EXACTLY N packets using DataLogger and save combined CSV")
#     parser.add_argument("--port", required=True, help="Serial port, e.g. COM5 or /dev/ttyUSB0")
#     parser.add_argument("--baud", type=int, default=115200, help="Baud rate (MUST match ESP32)")
#     parser.add_argument("--sample-rate", type=float, default=100.0, help="Hz for timestamps")
#     parser.add_argument("--duration", type=float, default=5.0, help="Used to compute packets if --packets not set")
#     parser.add_argument("--packets", type=int, default=0, help="Exact packets to capture (overrides duration*rate)")
#     parser.add_argument("--save-dir", default="saved_data", help="Output directory")
#     parser.add_argument("--filename", default="data_capture", help="Filename prefix")
#     parser.add_argument("--max-runtime", type=float, default=30.0, help="Safety timeout in seconds")
#     parser.add_argument("--include-indices", action="store_true", help="Include packet index column in CSV")
#     args = parser.parse_args()

#     # Calculate target packets
#     target = args.packets if args.packets > 0 else int(round(args.duration * args.sample_rate))
#     print(f"Target: {target} packets @ {args.sample_rate} Hz (baud {args.baud})")

#     # Create DataLogger instance
#     logger = DataLogger(
#         port=args.port,
#         baud_rate=args.baud,
#         num_channels=17  # 8 ADC + 9 IMU
#     )

#     print(f"Reading packets from {args.port}...")
    
#     # Read exact number of packets
#     result = logger.read_exact_packets(
#         target_packets=target,
#         max_runtime_s=args.max_runtime
#     )

#     # Display statistics
#     stats = result['stats']
#     print("\n" + "="*50)
#     print("Capture Statistics:")
#     print("="*50)
#     print(f"Valid packets:         {stats['valid_packets']}")
#     print(f"Bad checksum packets:  {stats['bad_checksum_packets']}")
#     print(f"Bad sync packets:      {stats['bad_sync_packets']}")
#     print(f"Bytes read:            {stats['bytes_read']:,}")
#     print(f"Elapsed time:          {stats['elapsed_s']:.3f} seconds")
#     if stats['elapsed_s'] > 0:
#         print(f"Effective rate:        {stats['valid_packets']/stats['elapsed_s']:.2f} packets/sec")
#     print("="*50 + "\n")

#     # Detect gaps
#     gap_info = logger.detect_gaps(result['indices'])
#     if gap_info['is_continuous'] and len(result['data']) == target:
#         print("✅ No index gaps detected. Packet stream is continuous.")
#     else:
#         print(f"⚠️  Missing packets (by index gaps): {gap_info['missing_count']}")
#         if gap_info['gaps']:
#             print("First few gaps (prev_idx -> next_idx : missing_between):")
#             for g in gap_info['gaps'][:10]:
#                 print(f"  {g[0]} -> {g[1]} : {g[2]}")

#     # Transpose data from list of rows to list of columns
#     # result['data'] is a list of rows, where each row is [adc0..adc7, imu0..imu8]
#     # We need list of columns for save_data
#     if len(result['data']) > 0:
#         num_channels = len(result['data'][0])
#         channel_data = [[row[i] for row in result['data']] for i in range(num_channels)]
#     else:
#         channel_data = [[] for _ in range(17)]

#     print(f"\nSaving {len(result['data'])} samples to {args.save_dir}...")
    
#     # Save data
#     created_files = logger.save_data(
#         filename_prefix=args.filename,
#         file_extension=".csv",
#         save_directory=args.save_dir,
#         skip_initial_zeros=False,  # Don't skip zeros since we're providing exact data
#         sample_rate=args.sample_rate,
#         timestamp_start=0.0,
#         combined=True,
#         include_indices=args.include_indices,
#         indices_data=result['indices'],
#         channel_data=channel_data
#     )

#     if created_files:
#         print(f"\n✅ Successfully saved: {created_files[0]}")
#         print(f"   Samples saved: {len(result['data'])} (requested {target})")
    
#     # Provide diagnostic info if target not reached
#     if len(result['data']) != target:
#         print("\n" + "="*50)
#         print("⚠️  WARNING: Did not reach target packet count")
#         print("="*50)
#         print("Possible causes:")
#         print("- ESP32 not producing data at expected rate (I2C delays, task overruns)")
#         print("- Baud rate mismatch or line noise causing checksum failures")
#         print("- USB-serial driver dropping bytes at high baud rates")
#         print("\nDiagnostics:")
#         if stats['bad_checksum_packets'] > len(result['data']) * 0.1:
#             print("  → HIGH checksum error rate suggests transport/parsing issues")
#         if gap_info['is_continuous']:
#             print("  → Indices are continuous - likely timed out before receiving enough")
#         else:
#             print("  → Index gaps detected - ESP32 may be dropping samples")
#         print("="*50)


# if __name__ == "__main__":
#     main()

"""
save_data.py
------------
Records EXACTLY N frames (FramePackets) using DataLogger class and saves combined CSV.

New protocol:
- ESP32 sends frames @ 100 Hz
- Each frame contains 10 ADC samples -> expanded to 1000 Hz rows by DataLogger
So:
- "samples" = expanded rows (1000 Hz)
- "frames"  = packets on wire (100 Hz)
"""

import argparse
from DataLogger import DataLogger


def main():
    parser = argparse.ArgumentParser(
        description="Record EXACTLY N frames (packets) and save expanded 1kHz combined CSV"
    )
    parser.add_argument("--port", required=True, help="Serial port, e.g. COM5 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=921600, help="Baud rate (MUST match ESP32)")

    # Targeting
    parser.add_argument("--adc-rate", type=float, default=1000.0, help="ADC sample rate for timestamps (default 1000 Hz)")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration in seconds (used if --samples/--frames not set)")
    parser.add_argument("--samples", type=int, default=0, help="Exact ADC samples to save (expanded rows at adc-rate)")
    parser.add_argument("--frames", type=int, default=0, help="Exact frames to capture from wire (each frame = 10 samples)")

    parser.add_argument("--save-dir", default="saved_data", help="Output directory")
    parser.add_argument("--filename", default="data_capture", help="Filename prefix")
    parser.add_argument("--max-runtime", type=float, default=30.0, help="Safety timeout in seconds")
    parser.add_argument("--include-indices", action="store_true", help="Include sample index column in CSV")
    args = parser.parse_args()

    SAMPLES_PER_FRAME = 10
    FRAME_RATE = 100.0  # protocol frame rate (informational)

    # Decide target frames
    if args.frames > 0:
        target_frames = args.frames
        target_samples = target_frames * SAMPLES_PER_FRAME
    elif args.samples > 0:
        target_samples = args.samples
        # ceil to ensure we get at least that many samples
        target_frames = (target_samples + (SAMPLES_PER_FRAME - 1)) // SAMPLES_PER_FRAME
    else:
        # duration-based
        target_samples = int(round(args.duration * args.adc_rate))
        target_frames = (target_samples + (SAMPLES_PER_FRAME - 1)) // SAMPLES_PER_FRAME

    print(
        f"Target: {target_frames} frames (~{target_frames * SAMPLES_PER_FRAME} samples) "
        f"@ {FRAME_RATE:.0f} Hz frames / {args.adc_rate:.0f} Hz ADC (baud {args.baud})"
    )

    logger = DataLogger(
        port=args.port,
        baud_rate=args.baud,
        num_channels=17  # 8 ADC + 9 IMU
    )

    print(f"Reading frames from {args.port}...")

    # Read exact number of frames from wire (each expands to 10 samples)
    result = logger.read_exact_packets(
        target_packets=target_frames,
        max_runtime_s=args.max_runtime
    )

    stats = result.get("stats", {})
    valid_frames = stats.get("valid_frames", 0)
    expanded_samples = stats.get("expanded_samples", len(result.get("data", [])))
    bad_crc_frames = stats.get("bad_crc_frames", 0)

    print("\n" + "=" * 50)
    print("Capture Statistics:")
    print("=" * 50)
    print(f"Valid frames:          {valid_frames}")
    print(f"Bad CRC frames:        {bad_crc_frames}")
    print(f"Expanded samples:      {expanded_samples}")
    print(f"Bytes read:            {stats.get('bytes_read', 0):,}")
    print(f"Elapsed time:          {stats.get('elapsed_s', 0.0):.3f} seconds")
    if stats.get("elapsed_s", 0.0) > 0:
        print(f"Effective frame rate:  {valid_frames / stats['elapsed_s']:.2f} frames/sec")
        print(f"Effective sample rate: {expanded_samples / stats['elapsed_s']:.2f} samples/sec")
    print("=" * 50 + "\n")

    # Gap detection on ADC sample indices (expanded)
    gap_info = logger.detect_gaps(result.get("indices", []))

    expected_samples_from_frames = valid_frames * SAMPLES_PER_FRAME
    if gap_info["is_continuous"] and expanded_samples == expected_samples_from_frames:
        print("✅ No index gaps detected. Expanded sample stream is continuous.")
    else:
        print(f"⚠️  Missing samples (by index gaps): {gap_info['missing_count']}")
        if gap_info["gaps"]:
            print("First few gaps (prev_idx -> next_idx : missing_between):")
            for g in gap_info["gaps"][:10]:
                print(f"  {g[0]} -> {g[1]} : {g[2]}")

    # If user asked for exact samples, trim (because we ceil frames)
    if args.samples > 0 and expanded_samples > args.samples:
        result["indices"] = result["indices"][:args.samples]
        result["data"] = result["data"][:args.samples]
        expanded_samples = args.samples

    # Transpose rows -> columns for save_data()
    if len(result["data"]) > 0:
        num_channels = len(result["data"][0])
        channel_data = [[row[i] for row in result["data"]] for i in range(num_channels)]
    else:
        channel_data = [[] for _ in range(17)]

    print(f"\nSaving {expanded_samples} samples to {args.save_dir}...")

    created_files = logger.save_data(
        filename_prefix=args.filename,
        file_extension=".csv",
        save_directory=args.save_dir,
        skip_initial_zeros=False,
        sample_rate=args.adc_rate,     # timestamps based on ADC rate (1000 Hz)
        timestamp_start=0.0,
        combined=True,
        include_indices=args.include_indices,
        indices_data=result["indices"],
        channel_data=channel_data
    )

    if created_files:
        print(f"\n✅ Successfully saved: {created_files[0]}")
        print(f"   Samples saved: {expanded_samples}")

    # Diagnostics if short
    if valid_frames != target_frames:
        print("\n" + "=" * 50)
        print("⚠️  WARNING: Did not reach target frame count")
        print("=" * 50)
        print("Possible causes:")
        print("- Serial bandwidth/driver issues at high baud")
        print("- Noise causing CRC failures")
        print("- ESP32 task overruns (rare with this framing, but possible)")
        print("\nDiagnostics:")
        if bad_crc_frames > max(1, valid_frames) * 0.1:
            print("  → HIGH CRC error rate suggests transport/parsing issues")
        if gap_info["is_continuous"]:
            print("  → Indices are continuous - likely timed out before receiving enough")
        else:
            print("  → Index gaps detected - ESP32 may be dropping frames/samples")
        print("=" * 50)


if __name__ == "__main__":
    main()
