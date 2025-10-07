"""
MMGFilterAnalyzer.py
--------------------
Master class for MMG signal filtering and analysis (IIR version)

Methods:
1) plot_raw_time_domain()
2) plot_raw_fft()
3) plot_raw_psd()
4) design_filter()
5) plot_filter_response()
6) apply_filter()
7) compare_filtered_vs_raw()
8) compute_latency_and_snr()

Usage example (see bottom or your notebook):
    from MMGFilterAnalyzer import MMGFilterAnalyzer
"""

import numpy as np
import matplotlib.pyplot as plt
from scipy import signal, integrate


class MMGFilterAnalyzer:
    def __init__(self, mmg: np.ndarray, fs: float):
        self.mmg = np.asarray(mmg, dtype=float)
        self.fs = float(fs)
        self.t = np.arange(len(self.mmg)) / self.fs

        # will be filled later
        self.sos_chain = None
        self.mmg_filt = None
        self.w = None
        self.h = None
        self.gd = None

        print(f"[INIT] MMG: {len(self.mmg)} samples @ {self.fs:.1f} Hz")

    # 1) Plot raw time-domain
    def plot_raw_time_domain(self, duration: float = 2.0):
        n = int(duration * self.fs)
        plt.figure(figsize=(10, 4))
        plt.plot(self.t[:n], self.mmg[:n])
        plt.xlabel("Time (s)")
        plt.ylabel("Amplitude (V)")
        plt.title(f"Raw MMG – Time Domain (first {duration:.1f} s)")
        plt.grid(True)
        plt.tight_layout()
        plt.show()

    # 2) Plot raw FFT
    def plot_raw_fft(self, max_hz: float = 250, db: bool = False):
        x = self.mmg - np.mean(self.mmg)
        N = len(x)
        if N == 0:
            print("[WARN] Empty signal.")
            return
        X = np.fft.rfft(x)
        f = np.fft.rfftfreq(N, d=1/self.fs)
        amp = np.abs(X) / N

        plt.figure(figsize=(9, 4))
        if db:
            plt.plot(f, 20*np.log10(np.maximum(amp, 1e-12)))
            plt.ylabel("Amplitude (dB)")
        else:
            plt.plot(f, amp)
            plt.ylabel("Amplitude")
        plt.xlim(0, max_hz)
        plt.xlabel("Frequency (Hz)")
        plt.title("Raw MMG – FFT Spectrum")
        plt.grid(True, ls=":")
        plt.tight_layout()
        plt.show()

    # 3) Plot raw PSD
    def plot_raw_psd(self, max_hz: float = 80, xtick_step: float = 5,
                     detrend_type: str = "constant",
                     nperseg: int = 4096, noverlap: int | None = None,
                     logy: bool = False):
        """Welch PSD of the raw (unfiltered) signal."""
        if detrend_type:
            mmg_raw = signal.detrend(self.mmg, type=detrend_type)
        else:
            mmg_raw = self.mmg

        N = len(mmg_raw)
        if N < 16:
            print("[WARN] Signal too short for PSD.")
            return
        nperseg = int(min(nperseg, N))
        if noverlap is None:
            noverlap = nperseg // 2

        f_raw, P_raw = signal.welch(
            mmg_raw, fs=self.fs,
            window='hann',
            nperseg=nperseg,
            noverlap=noverlap,
            detrend=False,         # already detrended above
            return_onesided=True,
            scaling='density'      # V^2/Hz
        )

        plt.figure(figsize=(9, 4))
        if logy:
            plt.semilogy(f_raw, P_raw)
        else:
            plt.plot(f_raw, P_raw)
        plt.xlim(0, max_hz)
        if xtick_step:
            plt.xticks(np.arange(0, max_hz + xtick_step, xtick_step))
        plt.title("Raw MMG – Power Spectral Density (Welch)")
        plt.xlabel("Frequency (Hz)")
        plt.ylabel("PSD (V²/Hz)")
        plt.grid(True, which='both', ls=':')
        plt.tight_layout()
        plt.show()

    # 4) Design filter (manual)
    def design_filter(self, hp_cut: float = 3, lp_cut: float = 60,
                      hp_order: int = 2, lp_order: int = 4,
                      lp_ftype: str = "butter"):
        """IIR chain: high-pass + low-pass (manual cutoffs)."""
        hp = float(hp_cut)
        lp = float(lp_cut)

        sos_hp = signal.iirfilter(
            N=hp_order, Wn=hp/(self.fs/2),
            btype='highpass', ftype='butter', output='sos'
        )
        if lp_ftype.lower() == "ellip":
            sos_lp = signal.iirfilter(
                N=lp_order, Wn=lp/(self.fs/2),
                btype='lowpass', ftype='ellip',
                rp=1, rs=60, output='sos'
            )
        else:
            sos_lp = signal.iirfilter(
                N=lp_order, Wn=lp/(self.fs/2),
                btype='lowpass', ftype='butter', output='sos'
            )
        self.sos_chain = np.vstack([sos_hp, sos_lp])
        print(f"[FILTER] HP({hp:.1f} Hz, ord {hp_order}) + LP({lp:.1f} Hz, ord {lp_order}) ({lp_ftype})")

    # 5) Plot filter response
    def plot_filter_response(self):
        if self.sos_chain is None:
            print("[WARN] Design filter first.")
            return
        self.w, self.h = signal.sosfreqz(self.sos_chain, worN=4096, fs=self.fs)
        phase = np.unwrap(np.angle(self.h))
        dw = np.gradient(self.w) * 2 * np.pi
        self.gd = -np.gradient(phase) / (dw + 1e-12)

        # Magnitude
        plt.figure(figsize=(9, 4))
        plt.plot(self.w, 20*np.log10(np.maximum(np.abs(self.h), 1e-12)))
        plt.xlim(0, 200)
        plt.xlabel("Frequency (Hz)")
        plt.ylabel("Magnitude (dB)")
        plt.title("Filter Magnitude Response (HP + LP)")
        plt.grid(True, ls=":")
        plt.tight_layout()
        plt.show()

        # Group delay
        plt.figure(figsize=(9, 4))
        plt.plot(self.w, self.gd*1000)
        plt.xlim(0, 120)
        plt.ylim(0, 25)
        plt.xlabel("Frequency (Hz)")
        plt.ylabel("Group Delay (ms)")
        plt.title("Group Delay Response")
        plt.grid(True, ls=":")
        plt.tight_layout()
        plt.show()

    # 6) Apply filter
    def apply_filter(self):
        if self.sos_chain is None:
            print("[WARN] Design filter first.")
            return
        self.mmg_filt = signal.sosfilt(self.sos_chain, self.mmg)
        print(f"[FILTER] Applied HP+LP chain to {len(self.mmg_filt)} samples.")

    # 7) Compare raw vs filtered (time + PSD)
    def compare_filtered_vs_raw(self, duration: float = 2.0, max_hz: float = 120):
        if self.mmg_filt is None:
            print("[WARN] Apply filter first.")
            return

        # Time-domain
        n = int(duration * self.fs)
        plt.figure(figsize=(10, 4))
        plt.plot(self.t[:n], self.mmg[:n], label="Raw", alpha=0.6)
        plt.plot(self.t[:n], self.mmg_filt[:n], label="Filtered", lw=1.2)
        plt.xlabel("Time (s)")
        plt.ylabel("Amplitude (V)")
        plt.title(f"MMG – Raw vs Filtered (first {duration:.1f} s)")
        plt.legend()
        plt.grid(True)
        plt.tight_layout()
        plt.show()

        # PSD comparison
        def _welch(x):
            N = len(x)
            nperseg = min(4096, N) if N > 0 else 1024
            noverlap = nperseg // 2
            return signal.welch(x, fs=self.fs, nperseg=nperseg, noverlap=noverlap)

        f_raw, P_raw = _welch(self.mmg)
        f_flt, P_flt = _welch(self.mmg_filt)

        plt.figure(figsize=(10, 4))
        plt.semilogy(f_raw, P_raw, label="Raw", alpha=0.7)
        plt.semilogy(f_flt, P_flt, label="Filtered", lw=1.2)
        plt.xlim(0, max_hz)
        plt.xlabel("Frequency (Hz)")
        plt.ylabel("PSD (V²/Hz)")
        plt.title("PSD Comparison – Raw vs Filtered")
        plt.legend()
        plt.grid(True, which="both", ls=":")
        plt.tight_layout()
        plt.show()

    # 8) Compute latency & SNR (minimal, theory-aligned)
    def compute_latency_and_snr(self, sig_band: tuple = (5, 60)):

        if any(v is None for v in [self.mmg_filt, self.sos_chain]):
            print("[WARN] Run design_filter() and apply_filter() first.")
            return

        # Ensure filter response/group delay are available
        if any(v is None for v in [self.w, self.h, self.gd]):
            self.plot_filter_response()

        # --- 1) Group delay (average over the band)
        band = (self.w >= sig_band[0]) & (self.w <= sig_band[1])
        gd_avg_ms = float(np.mean(self.gd[band]) * 1000.0)

        # --- 2) Empirical latency (vs zero-phase reference)
        zero_phase = signal.sosfiltfilt(self.sos_chain, self.mmg)
        xc = signal.correlate(zero_phase, self.mmg_filt, mode="full")
        lags = signal.correlation_lags(len(zero_phase), len(self.mmg_filt), mode="full")
        latency_ms = float(lags[np.argmax(xc)] / self.fs * 1000.0)

        # --- 3) ΔSNR improvement (band vs out-of-band)
        def bandpower(x, f1, f2):
            # Welch with sensible defaults and no deprecation warnings
            nperseg = min(4096, len(x)) if len(x) > 0 else 1024
            noverlap = nperseg // 2
            f, P = signal.welch(x, fs=self.fs, nperseg=nperseg, noverlap=noverlap)
            m = (f >= f1) & (f <= f2)
            return integrate.trapezoid(P[m], f[m]) if np.any(m) else 0.0

        # Raw SNR
        P_sig_raw   = bandpower(self.mmg, *sig_band)
        P_noise_raw = bandpower(self.mmg, 0, sig_band[0]) + bandpower(self.mmg, sig_band[1], self.fs/2)
        SNR_raw_dB  = 10 * np.log10((P_sig_raw + 1e-12) / (P_noise_raw + 1e-12))

        # Filtered SNR
        P_sig_flt   = bandpower(self.mmg_filt, *sig_band)
        P_noise_flt = bandpower(self.mmg_filt, 0, sig_band[0]) + bandpower(self.mmg_filt, sig_band[1], self.fs/2)
        SNR_flt_dB  = 10 * np.log10((P_sig_flt + 1e-12) / (P_noise_flt + 1e-12))

        dSNR_dB = float(SNR_flt_dB - SNR_raw_dB)
        # --- Output (only the essentials)
        print("[RESULTS]")
        print(f"  Group delay (avg {sig_band[0]}–{sig_band[1]} Hz): {gd_avg_ms:.2f} ms")
        print(f"  Empirical latency: {latency_ms:.2f} ms")
        print(f"  ΔSNR improvement: {dSNR_dB:.2f} dB")


if __name__ == "__main__":
    print("This module defines MMGFilterAnalyzer.\n")
