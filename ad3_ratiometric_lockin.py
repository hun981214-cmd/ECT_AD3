"""Continuous-drive ratiometric lock-in driver for AD3 two-coil ECT.

Measures the complex ratio pickup/drive (CH input / CH reference) while the
drive AWG runs continuously. Compared to the AWG-restart triggered path in
`ad3_triggered_lockin.py` this removes the per-row generator restart, which was
the dominant noise source (startup transients) and the main trigger of
`DptiIO` USB errors on this Jetson/AD3 setup.

Because both input channels are sampled by the same acquisition, the capture
start phase is common-mode and cancels exactly in the ratio, so no
AnalogOut-triggered acquisition is needed.

Wiring assumed by the defaults (EW-8SCT drive-pickup probe on the BNC
adapter, verified 2026-07-10):

- W1 (output channel 0): probe drive coil, continuous sine at 120 kHz
  (probe resonance measured 2026-07-10; pickup peak 114 mV per 1 V drive)
- CH1 (input channel 0): probe pickup coil (signal)
- W2 (output channel 1): twin-drive reference output, BNC-cabled to CH2
- CH2 (input channel 1): reference input

With a BNC T on the drive instead of the W2 twin cable, set
``reference_output_channel=None`` and tap CH2 from the drive node directly.
"""

from __future__ import annotations

import ctypes
import math
import time

import numpy as np

from ad3_triggered_lockin import TriggeredLockInMeasurement

HDWF_NONE = 0
DWF_STATE_DONE = 2
FUNC_SINE = 1
ANALOG_OUT_NODE_CARRIER = 0
ACQMODE_SINGLE = 0
FILTER_DECIMATE = 0
TRIGSRC_NONE = 0


class AD3ContinuousRatioLockIn:
    """Free-run captures of signal+reference with pooled per-window ratios.

    ``measure()`` returns the same ``TriggeredLockInMeasurement`` tuple layout
    as ``AD3TriggeredLockIn.measure()`` so logger/CLI code can treat both
    drivers interchangeably. In this driver ``raw_x``/``raw_y`` are the complex
    ratio signal/reference, which is dimensionless.
    """

    def __init__(
        self,
        output_channel: int = 0,
        input_channel: int = 0,
        reference_channel: int = 1,
        reference_output_channel: int | None = 1,
        drive_freq_hz: float = 120_000.0,
        drive_amplitude_v: float = 4.0,
        sample_rate_hz: float = 1_000_000.0,
        buffer_size: int = 16_384,
        signal_range_v: float = 5.0,
        reference_range_v: float | None = None,
        averages: int = 32,
        timeout_s: float = 3.0,
        settle_s: float = 0.5,
        lockin_window_cycles: int = 15,
        lockin_window_mad_threshold: float = 6.0,
        lockin_min_windows: int = 4,
        lockin_window_filter: bool = True,
        max_lockin_window_phase_std_deg: float = 5.0,
        min_reference_fraction: float = 0.5,
        clip_fraction_limit: float = 0.005,
        capture_retries: int = 3,
        capture_retry_delay_s: float = 0.1,
    ) -> None:
        self.output_channel = int(output_channel)
        self.input_channel = int(input_channel)
        self.reference_channel = int(reference_channel)
        if self.reference_channel == self.input_channel:
            raise ValueError("reference_channel must differ from input_channel")
        self.reference_output_channel = (
            None if reference_output_channel is None else int(reference_output_channel)
        )
        if self.reference_output_channel == self.output_channel:
            raise ValueError("reference_output_channel must differ from output_channel")
        self.drive_freq_hz = float(drive_freq_hz)
        self.drive_amplitude_v = float(drive_amplitude_v)
        self.sample_rate_hz = float(sample_rate_hz)
        self.buffer_size = int(buffer_size)
        self.signal_range_v = float(signal_range_v)
        if reference_range_v is None:
            # The low hardware range covers about +-2.55 V; put the reference
            # monitor on the high range when the drive swing would clip it.
            reference_range_v = 5.0 if self.drive_amplitude_v <= 1.2 else 50.0
        self.reference_range_v = float(reference_range_v)
        self.averages = max(1, int(averages))
        self.timeout_s = float(timeout_s)
        self.settle_s = float(settle_s)
        self.lockin_window_cycles = max(1, int(lockin_window_cycles))
        self.lockin_window_mad_threshold = float(lockin_window_mad_threshold)
        self.lockin_min_windows = max(1, int(lockin_min_windows))
        self.lockin_window_filter = bool(lockin_window_filter)
        self.max_lockin_window_phase_std_deg = float(max_lockin_window_phase_std_deg)
        self.min_reference_fraction = float(min_reference_fraction)
        self.clip_fraction_limit = float(clip_fraction_limit)
        self.capture_retries = max(0, int(capture_retries))
        self.capture_retry_delay_s = float(capture_retry_delay_s)

        self.dwf = ctypes.cdll.LoadLibrary("libdwf.so")
        self.hdwf = ctypes.c_int(HDWF_NONE)
        self._signal_buffer = (ctypes.c_double * self.buffer_size)()
        self._reference_buffer = (ctypes.c_double * self.buffer_size)()

        samples_per_cycle = self.sample_rate_hz / self.drive_freq_hz
        whole_cycles = int(math.floor(self.buffer_size / samples_per_cycle))
        if whole_cycles <= 0:
            raise ValueError("buffer does not contain a full drive cycle")
        demod_len = int(round(whole_cycles * samples_per_cycle))
        self._demod_slice = slice(0, demod_len)

        t = np.arange(demod_len, dtype=float) / self.sample_rate_hz
        self._ref_cos = np.cos(2.0 * np.pi * self.drive_freq_hz * t)
        self._ref_sin = -np.sin(2.0 * np.pi * self.drive_freq_hz * t)
        self._window = np.hanning(demod_len)
        self._freqs = np.fft.rfftfreq(demod_len, 1.0 / self.sample_rate_hz)
        self._drive_idx = int(np.argmin(np.abs(self._freqs - self.drive_freq_hz)))
        self._window_len = max(1, int(round(self.lockin_window_cycles * samples_per_cycle)))
        self._windows_per_capture = max(1, demod_len // self._window_len)

    # -- device lifecycle ---------------------------------------------------

    def _last_error(self) -> str:
        msg = ctypes.create_string_buffer(512)
        self.dwf.FDwfGetLastErrorMsg(msg)
        return msg.value.decode(errors="replace")

    def open(self) -> None:
        if self.dwf.FDwfDeviceOpen(ctypes.c_int(-1), ctypes.byref(self.hdwf)) != 1:
            raise RuntimeError(f"FDwfDeviceOpen failed: {self._last_error()}")
        if self.hdwf.value == HDWF_NONE:
            raise RuntimeError(f"failed to open AD3: {self._last_error()}")
        self.dwf.FDwfDeviceAutoConfigureSet(self.hdwf, ctypes.c_int(0))
        self.dwf.FDwfAnalogOutReset(self.hdwf, ctypes.c_int(-1))
        self.dwf.FDwfAnalogInReset(self.hdwf)
        self._configure_output()
        self._configure_input()
        time.sleep(self.settle_s)

    def recover(self, reopen: bool = False, delay_s: float = 0.25) -> None:
        if not reopen and self.hdwf.value != HDWF_NONE:
            try:
                self.dwf.FDwfAnalogOutConfigure(self.hdwf, ctypes.c_int(-1), ctypes.c_int(0))
                self.dwf.FDwfAnalogInConfigure(self.hdwf, ctypes.c_int(0), ctypes.c_int(0))
                time.sleep(max(0.0, delay_s))
                self.dwf.FDwfAnalogOutReset(self.hdwf, ctypes.c_int(-1))
                self.dwf.FDwfAnalogInReset(self.hdwf)
                self._configure_output()
                self._configure_input()
                time.sleep(self.settle_s)
                return
            except Exception:
                # Soft reset failed (stale/invalid handle after a USB glitch);
                # escalate to a hard reopen instead of surfacing the failure.
                pass
        try:
            self.close()
        except Exception:
            self.hdwf = ctypes.c_int(HDWF_NONE)
        time.sleep(max(0.0, delay_s))
        self.open()

    def close(self) -> None:
        if self.hdwf.value != HDWF_NONE:
            self.dwf.FDwfAnalogOutConfigure(self.hdwf, ctypes.c_int(-1), ctypes.c_int(0))
            self.dwf.FDwfAnalogOutReset(self.hdwf, ctypes.c_int(-1))
            self.dwf.FDwfDeviceClose(self.hdwf)
            self.hdwf = ctypes.c_int(HDWF_NONE)

    def __enter__(self) -> "AD3ContinuousRatioLockIn":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- configuration ------------------------------------------------------

    def _configure_output(self) -> None:
        node = ctypes.c_int(ANALOG_OUT_NODE_CARRIER)
        channels = [self.output_channel]
        if self.reference_output_channel is not None:
            # Twin drive: the reference AWG outputs an identical, clock- and
            # start-synchronized copy of the drive for CH-reference wiring
            # that taps the second BNC output instead of the drive node.
            channels.append(self.reference_output_channel)
        for channel in channels:
            out = ctypes.c_int(channel)
            self.dwf.FDwfAnalogOutNodeEnableSet(self.hdwf, out, node, ctypes.c_int(1))
            self.dwf.FDwfAnalogOutNodeFunctionSet(self.hdwf, out, node, ctypes.c_int(FUNC_SINE))
            self.dwf.FDwfAnalogOutNodeFrequencySet(
                self.hdwf, out, node, ctypes.c_double(self.drive_freq_hz)
            )
            self.dwf.FDwfAnalogOutNodeAmplitudeSet(
                self.hdwf, out, node, ctypes.c_double(self.drive_amplitude_v)
            )
            self.dwf.FDwfAnalogOutNodeOffsetSet(self.hdwf, out, node, ctypes.c_double(0.0))
            # Apply settings but hold the generator stopped.
            self.dwf.FDwfAnalogOutConfigure(self.hdwf, out, ctypes.c_int(0))
        # Continuous drive: start all configured channels in one call so the
        # drive/reference relative phase is fixed; never restarted per capture.
        start = ctypes.c_int(-1 if self.reference_output_channel is not None else self.output_channel)
        if self.dwf.FDwfAnalogOutConfigure(self.hdwf, start, ctypes.c_int(1)) != 1:
            raise RuntimeError(f"FDwfAnalogOutConfigure failed: {self._last_error()}")

    def _configure_input(self) -> None:
        self.dwf.FDwfAnalogInAcquisitionModeSet(self.hdwf, ctypes.c_int(ACQMODE_SINGLE))
        self.dwf.FDwfAnalogInFrequencySet(self.hdwf, ctypes.c_double(self.sample_rate_hz))
        self.dwf.FDwfAnalogInBufferSizeSet(self.hdwf, ctypes.c_int(self.buffer_size))
        self.dwf.FDwfAnalogInChannelEnableSet(self.hdwf, ctypes.c_int(-1), ctypes.c_int(0))
        for channel, range_v in (
            (self.input_channel, self.signal_range_v),
            (self.reference_channel, self.reference_range_v),
        ):
            ch = ctypes.c_int(channel)
            self.dwf.FDwfAnalogInChannelEnableSet(self.hdwf, ch, ctypes.c_int(1))
            self.dwf.FDwfAnalogInChannelRangeSet(self.hdwf, ch, ctypes.c_double(range_v))
            self.dwf.FDwfAnalogInChannelFilterSet(self.hdwf, ch, ctypes.c_int(FILTER_DECIMATE))
        self.dwf.FDwfAnalogInTriggerSourceSet(self.hdwf, ctypes.c_int(TRIGSRC_NONE))
        if self.dwf.FDwfAnalogInConfigure(self.hdwf, ctypes.c_int(1), ctypes.c_int(0)) != 1:
            raise RuntimeError(f"FDwfAnalogInConfigure failed: {self._last_error()}")
        actual_buffer = ctypes.c_int()
        self.dwf.FDwfAnalogInBufferSizeGet(self.hdwf, ctypes.byref(actual_buffer))
        if actual_buffer.value != self.buffer_size:
            raise RuntimeError(
                "AD3 analog-in buffer size was clamped by WaveForms: "
                f"requested {self.buffer_size}, actual {actual_buffer.value} "
                "(two enabled channels halve the shared buffer)"
            )
        self._range_limits = {}
        for channel in (self.input_channel, self.reference_channel):
            value = ctypes.c_double()
            self.dwf.FDwfAnalogInChannelRangeGet(
                self.hdwf, ctypes.c_int(channel), ctypes.byref(value)
            )
            self._range_limits[channel] = float(value.value) / 2.0

    # -- introspection ------------------------------------------------------

    def readback(self) -> dict[str, float]:
        node = ctypes.c_int(ANALOG_OUT_NODE_CARRIER)
        out = ctypes.c_int(self.output_channel)
        ao_freq = ctypes.c_double()
        ao_amp = ctypes.c_double()
        ai_freq = ctypes.c_double()
        ai_buffer = ctypes.c_int()
        sig_range = ctypes.c_double()
        ref_range = ctypes.c_double()
        self.dwf.FDwfAnalogOutNodeFrequencyGet(self.hdwf, out, node, ctypes.byref(ao_freq))
        self.dwf.FDwfAnalogOutNodeAmplitudeGet(self.hdwf, out, node, ctypes.byref(ao_amp))
        self.dwf.FDwfAnalogInFrequencyGet(self.hdwf, ctypes.byref(ai_freq))
        self.dwf.FDwfAnalogInBufferSizeGet(self.hdwf, ctypes.byref(ai_buffer))
        self.dwf.FDwfAnalogInChannelRangeGet(
            self.hdwf, ctypes.c_int(self.input_channel), ctypes.byref(sig_range)
        )
        self.dwf.FDwfAnalogInChannelRangeGet(
            self.hdwf, ctypes.c_int(self.reference_channel), ctypes.byref(ref_range)
        )
        return {
            "mode": "continuous_ratio",
            "ao_freq_hz": float(ao_freq.value),
            "ao_amp_v": float(ao_amp.value),
            "ai_freq_hz": float(ai_freq.value),
            "ai_buffer": float(ai_buffer.value),
            "ai_range_v": float(sig_range.value),
            "reference_channel": float(self.reference_channel),
            "reference_range_v": float(ref_range.value),
            "trigger_source": float(TRIGSRC_NONE),
        }

    def read_board_status(self) -> dict[str, float]:
        values = {
            "ad3_pcb_temp_c": float("nan"),
            "ad3_fpga_temp_c": float("nan"),
            "ad3_usb_v": float("nan"),
            "ad3_usb_a": float("nan"),
        }
        try:
            self.dwf.FDwfAnalogIOStatus(self.hdwf)
            for name, channel, node in (
                ("ad3_pcb_temp_c", 2, 0),
                ("ad3_fpga_temp_c", 2, 1),
                ("ad3_usb_v", 2, 2),
                ("ad3_usb_a", 2, 3),
            ):
                value = ctypes.c_double(float("nan"))
                self.dwf.FDwfAnalogIOChannelNodeStatus(
                    self.hdwf,
                    ctypes.c_int(channel),
                    ctypes.c_int(node),
                    ctypes.byref(value),
                )
                values[name] = float(value.value)
        except Exception:
            pass
        return values

    # -- measurement --------------------------------------------------------

    def _capture_one(self) -> tuple[np.ndarray, np.ndarray]:
        if self.dwf.FDwfAnalogInConfigure(self.hdwf, ctypes.c_int(0), ctypes.c_int(1)) != 1:
            raise RuntimeError(f"FDwfAnalogInConfigure failed: {self._last_error()}")
        status = ctypes.c_byte(0)
        started = time.monotonic()
        while True:
            if self.dwf.FDwfAnalogInStatus(self.hdwf, ctypes.c_int(1), ctypes.byref(status)) != 1:
                raise RuntimeError(f"FDwfAnalogInStatus failed: {self._last_error()}")
            if status.value == DWF_STATE_DONE:
                break
            if time.monotonic() - started > self.timeout_s:
                raise TimeoutError("AD3 ratiometric capture timed out")
            time.sleep(0.0005)
        self.dwf.FDwfAnalogInStatusData(
            self.hdwf,
            ctypes.c_int(self.input_channel),
            self._signal_buffer,
            ctypes.c_int(self.buffer_size),
        )
        self.dwf.FDwfAnalogInStatusData(
            self.hdwf,
            ctypes.c_int(self.reference_channel),
            self._reference_buffer,
            ctypes.c_int(self.buffer_size),
        )
        signal = np.ctypeslib.as_array(self._signal_buffer).copy()
        reference = np.ctypeslib.as_array(self._reference_buffer).copy()
        return signal, reference

    def _check_clipping(self, wave: np.ndarray, channel: int, label: str) -> None:
        limit = self._range_limits.get(channel)
        if not limit or not math.isfinite(limit):
            return
        frac = float(np.mean(np.abs(wave) >= 0.98 * limit))
        if frac > self.clip_fraction_limit:
            raise RuntimeError(
                f"{label} channel clipping: {frac * 100:.2f}% of samples at "
                f">=98% of +-{limit:.3f} V input range"
            )

    def _window_pairs(self, signal: np.ndarray, reference: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        length = self._windows_per_capture * self._window_len
        s = signal[self._demod_slice][:length].reshape(self._windows_per_capture, self._window_len)
        r = reference[self._demod_slice][:length].reshape(self._windows_per_capture, self._window_len)
        s = s - s.mean(axis=1, keepdims=True)
        r = r - r.mean(axis=1, keepdims=True)
        cos = self._ref_cos[:length].reshape(self._windows_per_capture, self._window_len)
        sin = self._ref_sin[:length].reshape(self._windows_per_capture, self._window_len)
        scale = 2.0 / self._window_len
        s_win = scale * ((s * cos).sum(axis=1) + 1j * (s * sin).sum(axis=1))
        r_win = scale * ((r * cos).sum(axis=1) + 1j * (r * sin).sum(axis=1))
        return s_win, r_win

    @staticmethod
    def _robust_complex_mask(values: np.ndarray, threshold: float) -> np.ndarray:
        if values.size <= 2:
            return np.ones(values.size, dtype=bool)
        center = complex(float(np.median(values.real)), float(np.median(values.imag)))
        distances = np.abs(values - center)
        median_distance = float(np.median(distances))
        mad = float(np.median(np.abs(distances - median_distance)))
        scale = 1.4826 * mad
        if not math.isfinite(scale) or scale <= 1e-15:
            return np.ones(values.size, dtype=bool)
        return distances <= median_distance + threshold * scale

    def measure(self) -> tuple[TriggeredLockInMeasurement, np.ndarray, np.ndarray, np.ndarray]:
        signal_windows: list[np.ndarray] = []
        reference_windows: list[np.ndarray] = []
        vpps: list[float] = []
        means: list[float] = []
        stds: list[float] = []
        drive_amps: list[float] = []
        drive_phases: list[float] = []
        peak_freqs: list[float] = []
        peak_amps: list[float] = []
        amp_spectra: list[np.ndarray] = []
        last_signal: np.ndarray | None = None

        # The continuous drive makes captures independent, so a transient USB
        # glitch only costs the failed capture: retry it in place instead of
        # discarding the whole row.
        capture_failures = 0
        captures_done = 0
        while captures_done < self.averages:
            try:
                signal, reference = self._capture_one()
            except (RuntimeError, TimeoutError):
                capture_failures += 1
                if capture_failures > self.capture_retries:
                    raise
                time.sleep(self.capture_retry_delay_s)
                continue
            captures_done += 1
            self._check_clipping(signal, self.input_channel, "signal")
            self._check_clipping(reference, self.reference_channel, "reference")
            s_win, r_win = self._window_pairs(signal, reference)
            # Free-run captures start at a random phase of the continuous
            # drive. Rotate each capture by its own reference phase before
            # pooling so component statistics are meaningful: the reference
            # lands on the positive real axis and the signal phase becomes the
            # reference-relative (= ratio) phase. |s|, |r|, and s/r are
            # invariant under this rotation.
            ref_vector = complex(np.sum(r_win))
            if abs(ref_vector) > 1e-15:
                rotation = np.conj(ref_vector) / abs(ref_vector)
                capture_phase_deg = float(np.degrees(np.angle(ref_vector)))
            else:
                rotation = 1.0 + 0.0j
                capture_phase_deg = 0.0
            signal_windows.append(s_win * rotation)
            reference_windows.append(r_win * rotation)
            last_signal = signal

            stable = signal[self._demod_slice]
            centered = stable - float(np.mean(stable))
            vpps.append(float(np.percentile(stable, 95) - np.percentile(stable, 5)))
            means.append(float(np.mean(stable)))
            stds.append(float(np.std(stable, ddof=1)))

            spectrum = np.fft.rfft(centered * self._window)
            coherent_gain = float(np.sum(self._window) / centered.size)
            amp = (2.0 / (centered.size * coherent_gain)) * np.abs(spectrum)
            phase = np.angle(spectrum, deg=True)
            search = self._freqs >= 100.0
            peak_idx_rel = int(np.argmax(amp[search]))
            peak_idx = int(np.flatnonzero(search)[peak_idx_rel])
            drive_amps.append(float(amp[self._drive_idx]))
            # The absolute FFT phase at the drive bin is random per free-run
            # capture; referencing it to this capture's reference phase makes
            # it a stable diagnostic (constant offset, comparable row to row).
            drive_phases.append(float(phase[self._drive_idx]) - capture_phase_deg)
            peak_freqs.append(float(self._freqs[peak_idx]))
            peak_amps.append(float(amp[peak_idx]))
            amp_spectra.append(amp)

        s_all = np.concatenate(signal_windows)
        r_all = np.concatenate(reference_windows)
        reference_magnitude_med = float(np.median(np.abs(r_all)))
        min_reference = self.min_reference_fraction * self.drive_amplitude_v
        if reference_magnitude_med < min_reference:
            raise RuntimeError(
                "reference channel lock-in magnitude too low: "
                f"{reference_magnitude_med:.4f} V < {min_reference:.4f} V "
                "(drive disconnected, current-limited, or wrong wiring)"
            )
        valid = np.abs(r_all) > 1e-12
        ratios = s_all[valid] / r_all[valid]

        total_windows = int(ratios.size)
        if self.lockin_window_filter and total_windows >= self.lockin_min_windows:
            keep = self._robust_complex_mask(ratios, self.lockin_window_mad_threshold)
            if int(np.sum(keep)) < self.lockin_min_windows:
                keep = np.ones(total_windows, dtype=bool)
        else:
            keep = np.ones(total_windows, dtype=bool)
        kept = ratios[keep]

        raw_x = float(np.median(kept.real))
        raw_y = float(np.median(kept.imag))
        kept_magnitudes = np.abs(kept)
        mag_mean = float(np.mean(kept_magnitudes)) if kept_magnitudes.size else float("nan")
        mag_std = float(np.std(kept_magnitudes, ddof=1)) if kept_magnitudes.size > 1 else 0.0
        kept_phases = np.unwrap(np.angle(kept))
        phase_std_deg = (
            float(np.std(np.degrees(kept_phases), ddof=1)) if kept_phases.size > 1 else 0.0
        )
        if (
            self.max_lockin_window_phase_std_deg > 0
            and math.isfinite(phase_std_deg)
            and phase_std_deg > self.max_lockin_window_phase_std_deg
        ):
            raise RuntimeError(
                "ratio window phase spread too high: "
                f"{phase_std_deg:.3f} deg > {self.max_lockin_window_phase_std_deg:.3f} deg"
            )

        # s_all/r_all are reference-phase-aligned per capture, so component
        # medians are meaningful: reference_phase_deg is ~0 by construction
        # and signal_phase_deg is the reference-relative (ratio) phase.
        # Magnitudes use median(|.|), which is rotation-invariant and robust.
        signal_complex = complex(
            float(np.median(s_all.real)), float(np.median(s_all.imag))
        )
        reference_complex = complex(
            float(np.median(r_all.real)), float(np.median(r_all.imag))
        )
        signal_magnitude = float(np.median(np.abs(s_all)))
        reference_magnitude = float(np.median(np.abs(r_all)))

        measurement = TriggeredLockInMeasurement(
            raw_x=raw_x,
            raw_y=raw_y,
            magnitude=float(math.hypot(raw_x, raw_y)),
            phase_deg=float(math.degrees(math.atan2(raw_y, raw_x))),
            signal_raw_x=float(signal_complex.real),
            signal_raw_y=float(signal_complex.imag),
            signal_magnitude=signal_magnitude,
            signal_phase_deg=float(math.degrees(math.atan2(signal_complex.imag, signal_complex.real))),
            reference_raw_x=float(reference_complex.real),
            reference_raw_y=float(reference_complex.imag),
            reference_magnitude=reference_magnitude,
            reference_phase_deg=float(
                math.degrees(math.atan2(reference_complex.imag, reference_complex.real))
            ),
            vpp=float(np.median(vpps)),
            mean_v=float(np.median(means)),
            std_v=float(np.median(stds)),
            drive_bin_amp=float(np.median(drive_amps)),
            drive_bin_phase_deg=float(np.median(drive_phases)),
            peak_freq_hz=float(np.median(peak_freqs)),
            peak_amp=float(np.median(peak_amps)),
            snr_drive_to_peak=(
                float(np.median(drive_amps) / np.median(peak_amps))
                if np.median(peak_amps) > 0
                else float("nan")
            ),
            sample_rate_hz=self.sample_rate_hz,
            buffer_size=self.buffer_size,
            lockin_window_count=total_windows,
            lockin_window_kept=int(np.sum(keep)),
            lockin_window_rejected=int(total_windows - np.sum(keep)),
            lockin_window_phase_std_deg=phase_std_deg,
            lockin_window_magnitude_rel_std=(
                mag_std / abs(mag_mean) if abs(mag_mean) > 1e-15 else float("nan")
            ),
        )
        assert last_signal is not None
        amp_spectrum = np.median(np.vstack(amp_spectra), axis=0)
        return measurement, last_signal, self._freqs, amp_spectrum
