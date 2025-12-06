"""
data_logger_test.py
-------------------
Small script to record 5 seconds of data from `DataLogger` and save CSV files.

Features:
- Connect to real serial port or run in simulation mode when serial is unavailable.
- Saves per-channel CSVs with timestamps (or a combined CSV when `--combined` is used).

Usage (from cmd.exe):
    python data_logger_test.py --port COM8 --baud 115200

For simulation (no serial required):
    python data_logger_test.py --simulate

"""
import time
import os
import argparse
import importlib
import numpy as np


def main():
    parser = argparse.ArgumentParser(description='Record 5 seconds from DataLogger and save CSVs')
    parser.add_argument('--port', default='COM8', help='Serial port (e.g. COM8)')
    parser.add_argument('--baud', type=int, default=115200, help='Baud rate')
    parser.add_argument('--num-ch', type=int, default=17, help='Number of channels expected')
    parser.add_argument('--duration', type=float, default=5.0, help='Duration to record (seconds)')
    parser.add_argument('--sample-rate', type=float, default=1000.0, help='Sampling rate in Hz for timestamps')
    parser.add_argument('--save-dir', default='saved_data', help='Directory to save CSVs')
    parser.add_argument('--simulate', action='store_true', help='Simulate data rather than open serial')
    parser.add_argument('--combined', action='store_true', help='Save single combined CSV instead of per-channel files')
    args = parser.parse_args()

    # Import DataLogger after parsing so a user can edit the module then re-run the script.
    try:
        import DataLogger as _DL
        importlib.reload(_DL)
        DataLogger = _DL.DataLogger
    except Exception as e:
        print('Error importing DataLogger:', e)
        return

    # Buffer length: keep a bit more than needed to be safe
    needed_samples = int(np.ceil(args.sample_rate * args.duration))
    buffer_len = max(needed_samples + 100, 2000)

    logger = DataLogger(port=args.port,
                        baud_rate=args.baud,
                        num_channels=args.num_ch,
                        buffer_length=buffer_len,
                        samples_per_event=2)

    # Ensure save directory exists
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    if args.simulate:
        print('Simulation mode: generating synthetic data')
        # Fill buffers with synthetic data (sine waves + noise)
        t = np.arange(needed_samples) / float(args.sample_rate)
        freqs = [5.0 + i for i in range(args.num_ch)]
        for ch in range(args.num_ch):
            # create a channel signal and write into the channel deque
            sig = 0.5 * np.sin(2 * np.pi * freqs[ch] * t) + 0.05 * np.random.randn(len(t))
            # Replace existing deque contents with the simulated samples (oldest->newest)
            dq = logger.channels[ch]
            dq.clear()
            for v in sig:
                dq.append(float(v))

        print(f'Simulated {needed_samples} samples per channel')

    else:
        print('Starting real logging...')
        logger.clear_buffers()
        logger.start_logging()
        # wait a little for thread to open serial
        time.sleep(2.0)

        print(f'Collecting data for {args.duration:.1f} seconds...')
        start_t = time.time()
        while (time.time() - start_t) < args.duration:
            time.sleep(0.01)
            logger.update_buffers()

        # final drain
        logger.update_buffers()
        logger.stop_logging()
        print('Logging stopped')

    # Ensure save_data is bound to this instance (useful if module was edited)
    try:
        importlib.reload(_DL)
        logger.save_data = _DL.DataLogger.save_data.__get__(logger, _DL.DataLogger)
    except Exception:
        # If reload fails, continue with existing method (will raise if not present)
        pass

    print('\nSaving data to files...')
    try:
        saved = logger.save_data(filename_prefix='test_',
                                 file_extension='.csv',
                                 save_directory=args.save_dir,
                                 skip_initial_zeros=True,
                                 sample_rate=args.sample_rate,
                                 timestamp_start=0.0,
                                 combined=args.combined)
        print(f'Done. Saved {len(saved)} files:')
        for f in saved:
            print(' -', f)
    except TypeError as e:
        print('save_data signature mismatch. Try restarting Python/kernel or update DataLogger.py.\n', e)
    except Exception as e:
        print('Error while saving data:', e)


if __name__ == '__main__':
    main()
