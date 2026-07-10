"""
Triggered software lock-in driver for AD3 absolute ECT.

This combines the part that worked well in the old SF scripts
(`trigsrcAnalogOut1`, arm AnalogIn before starting W1) with explicit lock-in
demodulation at the drive frequency.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import math
import time

import numpy as np


HDWF_NONE = 0
DWF_STATE_DONE = 2
FUNC_SINE = 1
ANALOG_OUT_NODE_CARRIER = 0
ACQMODE_SINGLE = 0
FILTER_DECIMATE = 0
TRIGSRC_ANALOG_OUT = {0: 7, 1: 8}


@dataclass
class TriggeredLockInMeasurement:
    raw_x: float
    raw_y: float
    magnitude: float
    phase_deg: float
    signal_raw_x: float
    signal_raw_y: float
    signal_magnitude: float
    signal_phase_deg: float
    reference_raw_x: float
    reference_raw_y: float
    reference_magnitude: float
    reference_phase_deg: float
    vpp: float
    mean_v: float
    std_v: float
    drive_bin_amp: float
    drive_bin_phase_deg: float
    peak_freq_hz: float
    peak_amp: float
    snr_drive_to_peak: float
    sample_rate_hz: float
    buffer_size: int
    lockin_window_count: int
    lockin_window_kept: int
    lockin_window_rejected: int
    lockin_window_phase_std_deg: float
    lockin_window_magnitude_rel_std: float


class AD3TriggeredLockIn:
    def __init__(
        self,
        output_channel: int = 0,
        input_channel: int = 0,
        reference_channel: int | None = None,
        reference_output_channel: int | None = None,
        drive_freq_hz: float = 10_000.0,
        drive_amplitude_v: float = 1.0,
        sample_rate_hz: float = 1_000_000.0,
        buffer_size: int = 10_000,
        analog_range_v: float = 5.0,
        averages: int = 1,
        timeout_s: float = 3.0,
        settle_s: float = 0.2,
        discard_s: float = 0.001,
        lockin_window_cycles: int = 15,
        lockin_window_mad_threshold: float = 6.0,
        lockin_min_windows: int = 4,
        lockin_window_filter: bool = True,
        max_lockin_window_phase_std_deg: float = 5.0,
        clip_fraction_limit: float = 0.005,
    ) -> None:
        self.output_channel = int(output_channel)
        self.input_channel = int(input_channel)
        self.reference_channel = None if reference_channel is None else int(reference_channel)
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
        self.analog_range_v = float(analog_range_v)
        self.averages = int(averages)
        self.timeout_s = float(timeout_s)
        self.settle_s = float(settle_s)
        self.discard_s = float(discard_s)
        self.lockin_window_cycles = max(1, int(lockin_window_cycles))
        self.lockin_window_mad_threshold = float(lockin_window_mad_threshold)
        self.lockin_min_windows = max(1, int(lockin_min_windows))
        self.lockin_window_filter = bool(lockin_window_filter)
        self.max_lockin_window_phase_std_deg = float(max_lockin_window_phase_std_deg)
        self.clip_fraction_limit = float(clip_fraction_limit)
        self._range_limits: dict[int, float] = {}
        self.dwf = ctypes.cdll.LoadLibrary("libdwf.so")
        self.hdwf = ctypes.c_int(HDWF_NONE)
        self._buffer = (ctypes.c_double * self.buffer_size)()
        self._reference_buffer = (ctypes.c_double * self.buffer_size)()

        demod_start = max(0, int(round(self.discard_s * self.sample_rate_hz)))
        samples_per_cycle = self.sample_rate_hz / self.drive_freq_hz
        demod_len = self.buffer_size - demod_start
        if demod_len <= 0:
            raise ValueError("discard_s leaves no samples for demodulation")
        whole_cycles = int(math.floor(demod_len / samples_per_cycle))
        if whole_cycles <= 0:
            raise ValueError("buffer does not contain a full post-discard drive cycle")
        demod_len = int(round(whole_cycles * samples_per_cycle))
        self._demod_slice = slice(demod_start, demod_start + demod_len)

        t = np.arange(demod_len, dtype=float) / self.sample_rate_hz
        self._ref_cos = np.cos(2.0 * np.pi * self.drive_freq_hz * t)
        self._ref_sin = -np.sin(2.0 * np.pi * self.drive_freq_hz * t)
        self._window = np.hanning(demod_len)
        self._freqs = np.fft.rfftfreq(demod_len, 1.0 / self.sample_rate_hz)
        self._drive_idx = int(np.argmin(np.abs(self._freqs - self.drive_freq_hz)))
        self._window_len = max(
            1,
            int(round(self.lockin_window_cycles * samples_per_cycle)),
        )
        self._window_count = max(1, demod_len // self._window_len)

    def _demodulate_complex(self, stable: np.ndarray) -> complex:
        centered = stable - float(np.mean(stable))
        raw_x = float((2.0 / centered.size) * np.dot(centered, self._ref_cos))
        raw_y = float((2.0 / centered.size) * np.dot(centered, self._ref_sin))
        return complex(raw_x, raw_y)

    @staticmethod
    def _robust_xy_mask(xs: np.ndarray, ys: np.ndarray, threshold: float) -> np.ndarray:
        if xs.size <= 2:
            return np.ones(xs.size, dtype=bool)
        center_x = float(np.median(xs))
        center_y = float(np.median(ys))
        distances = np.hypot(xs - center_x, ys - center_y)
        median_distance = float(np.median(distances))
        mad = float(np.median(np.abs(distances - median_distance)))
        scale = 1.4826 * mad
        if not math.isfinite(scale) or scale <= 1e-15:
            return np.ones(xs.size, dtype=bool)
        return distances <= median_distance + threshold * scale

    def _phase_std_deg(self, xs: np.ndarray, ys: np.ndarray) -> float:
        if xs.size <= 1:
            return 0.0
        phases = np.unwrap(np.arctan2(ys, xs))
        return float(np.std(np.degrees(phases), ddof=1))

    def _demodulate_lockin_windows(self, stable: np.ndarray) -> dict[str, float]:
        full = self._demodulate_complex(stable)

        usable_len = self._window_count * self._window_len
        if (
            not self.lockin_window_filter
            or self._window_count < self.lockin_min_windows
            or usable_len <= 0
        ):
            return {
                "raw_x": float(full.real),
                "raw_y": float(full.imag),
                "window_count": self._window_count,
                "window_kept": self._window_count,
                "window_rejected": 0,
                "window_phase_std_deg": float("nan"),
                "window_magnitude_rel_std": float("nan"),
            }

        xs = []
        ys = []
        for start in range(0, usable_len, self._window_len):
            stop = start + self._window_len
            segment = stable[start:stop]
            segment_centered = segment - float(np.mean(segment))
            ref_cos = self._ref_cos[start:stop]
            ref_sin = self._ref_sin[start:stop]
            xs.append(float((2.0 / segment_centered.size) * np.dot(segment_centered, ref_cos)))
            ys.append(float((2.0 / segment_centered.size) * np.dot(segment_centered, ref_sin)))

        xs_arr = np.array(xs, dtype=float)
        ys_arr = np.array(ys, dtype=float)
        keep = self._robust_xy_mask(xs_arr, ys_arr, self.lockin_window_mad_threshold)
        if int(np.sum(keep)) < self.lockin_min_windows:
            keep = np.ones(xs_arr.size, dtype=bool)

        kept_x = xs_arr[keep]
        kept_y = ys_arr[keep]
        raw_x = float(np.median(kept_x))
        raw_y = float(np.median(kept_y))
        magnitudes = np.hypot(kept_x, kept_y)
        mag_mean = float(np.mean(magnitudes)) if magnitudes.size else float("nan")
        mag_std = float(np.std(magnitudes, ddof=1)) if magnitudes.size > 1 else 0.0
        phase_std_deg = self._phase_std_deg(kept_x, kept_y)
        if (
            self.max_lockin_window_phase_std_deg > 0
            and math.isfinite(phase_std_deg)
            and phase_std_deg > self.max_lockin_window_phase_std_deg
        ):
            raise RuntimeError(
                "lock-in window phase spread too high: "
                f"{phase_std_deg:.3f} deg > {self.max_lockin_window_phase_std_deg:.3f} deg"
            )
        return {
            "raw_x": raw_x,
            "raw_y": raw_y,
            "window_count": int(xs_arr.size),
            "window_kept": int(np.sum(keep)),
            "window_rejected": int(xs_arr.size - np.sum(keep)),
            "window_phase_std_deg": phase_std_deg,
            "window_magnitude_rel_std": mag_std / abs(mag_mean) if abs(mag_mean) > 1e-15 else float("nan"),
        }

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
        """Recover WaveForms state after a failed capture/configure call."""
        if reopen:
            self.close()
            time.sleep(max(0.0, delay_s))
            self.open()
            return

        if self.hdwf.value == HDWF_NONE:
            self.open()
            return

        out = ctypes.c_int(-1 if self.reference_output_channel is not None else self.output_channel)
        try:
            self.dwf.FDwfAnalogOutConfigure(self.hdwf, out, ctypes.c_int(0))
        except Exception:
            pass
        try:
            self.dwf.FDwfAnalogInConfigure(self.hdwf, ctypes.c_int(0), ctypes.c_int(0))
        except Exception:
            pass
        time.sleep(max(0.0, delay_s))
        self.dwf.FDwfAnalogOutReset(self.hdwf, ctypes.c_int(-1))
        self.dwf.FDwfAnalogInReset(self.hdwf)
        self._configure_output()
        self._configure_input()
        time.sleep(self.settle_s)

    def _configure_output(self) -> None:
        node = ctypes.c_int(ANALOG_OUT_NODE_CARRIER)
        channels = [self.output_channel]
        if self.reference_output_channel is not None:
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
            # Apply configuration but keep the generator stopped until each capture.
            self.dwf.FDwfAnalogOutConfigure(self.hdwf, out, ctypes.c_int(0))

    def _configure_input(self) -> None:
        inp = ctypes.c_int(self.input_channel)
        self.dwf.FDwfAnalogInAcquisitionModeSet(self.hdwf, ctypes.c_int(ACQMODE_SINGLE))
        self.dwf.FDwfAnalogInFrequencySet(self.hdwf, ctypes.c_double(self.sample_rate_hz))
        self.dwf.FDwfAnalogInBufferSizeSet(self.hdwf, ctypes.c_int(self.buffer_size))
        self.dwf.FDwfAnalogInChannelEnableSet(self.hdwf, ctypes.c_int(-1), ctypes.c_int(0))
        self.dwf.FDwfAnalogInChannelEnableSet(self.hdwf, inp, ctypes.c_int(1))
        self.dwf.FDwfAnalogInChannelRangeSet(
            self.hdwf, inp, ctypes.c_double(self.analog_range_v)
        )
        self.dwf.FDwfAnalogInChannelFilterSet(self.hdwf, inp, ctypes.c_int(FILTER_DECIMATE))
        if self.reference_channel is not None:
            ref = ctypes.c_int(self.reference_channel)
            self.dwf.FDwfAnalogInChannelEnableSet(self.hdwf, ref, ctypes.c_int(1))
            self.dwf.FDwfAnalogInChannelRangeSet(
                self.hdwf, ref, ctypes.c_double(self.analog_range_v)
            )
            self.dwf.FDwfAnalogInChannelFilterSet(self.hdwf, ref, ctypes.c_int(FILTER_DECIMATE))
        self.dwf.FDwfAnalogInTriggerSourceSet(
            self.hdwf, ctypes.c_int(TRIGSRC_ANALOG_OUT[self.output_channel])
        )
        self.dwf.FDwfAnalogInTriggerAutoTimeoutSet(self.hdwf, ctypes.c_double(1.0))
        self.dwf.FDwfAnalogInTriggerTypeSet(self.hdwf, ctypes.c_int(0))
        self.dwf.FDwfAnalogInTriggerConditionSet(self.hdwf, ctypes.c_int(0))
        if self.dwf.FDwfAnalogInConfigure(self.hdwf, ctypes.c_int(1), ctypes.c_int(0)) != 1:
            raise RuntimeError(f"FDwfAnalogInConfigure failed: {self._last_error()}")
        actual_buffer = ctypes.c_int()
        self.dwf.FDwfAnalogInBufferSizeGet(self.hdwf, ctypes.byref(actual_buffer))
        if actual_buffer.value != self.buffer_size:
            raise RuntimeError(
                "AD3 analog-in buffer size was clamped by WaveForms: "
                f"requested {self.buffer_size}, actual {actual_buffer.value}"
            )
        self._range_limits = {}
        channels = [self.input_channel]
        if self.reference_channel is not None:
            channels.append(self.reference_channel)
        for channel in channels:
            value = ctypes.c_double()
            self.dwf.FDwfAnalogInChannelRangeGet(
                self.hdwf, ctypes.c_int(channel), ctypes.byref(value)
            )
            self._range_limits[channel] = float(value.value) / 2.0

    def _check_clipping(self, wave: np.ndarray, channel: int, label: str) -> None:
        limit = self._range_limits.get(channel)
        if not limit or not math.isfinite(limit) or self.clip_fraction_limit <= 0:
            return
        frac = float(np.mean(np.abs(wave) >= 0.98 * limit))
        if frac > self.clip_fraction_limit:
            raise RuntimeError(
                f"{label} channel clipping: {frac * 100:.2f}% of samples at "
                f">=98% of +-{limit:.3f} V input range; reduce drive amplitude "
                "or raise the channel range"
            )

    def readback(self) -> dict[str, float]:
        out = ctypes.c_int(self.output_channel)
        inp = ctypes.c_int(self.input_channel)
        node = ctypes.c_int(ANALOG_OUT_NODE_CARRIER)
        ao_freq = ctypes.c_double()
        ao_amp = ctypes.c_double()
        ref_ao_freq = ctypes.c_double(float("nan"))
        ref_ao_amp = ctypes.c_double(float("nan"))
        ai_freq = ctypes.c_double()
        ai_range = ctypes.c_double()
        ref_range = ctypes.c_double(float("nan"))
        ai_buffer = ctypes.c_int()
        trigger_source = ctypes.c_byte()
        self.dwf.FDwfAnalogOutNodeFrequencyGet(self.hdwf, out, node, ctypes.byref(ao_freq))
        self.dwf.FDwfAnalogOutNodeAmplitudeGet(self.hdwf, out, node, ctypes.byref(ao_amp))
        if self.reference_output_channel is not None:
            ref_out = ctypes.c_int(self.reference_output_channel)
            self.dwf.FDwfAnalogOutNodeFrequencyGet(
                self.hdwf, ref_out, node, ctypes.byref(ref_ao_freq)
            )
            self.dwf.FDwfAnalogOutNodeAmplitudeGet(
                self.hdwf, ref_out, node, ctypes.byref(ref_ao_amp)
            )
        self.dwf.FDwfAnalogInFrequencyGet(self.hdwf, ctypes.byref(ai_freq))
        self.dwf.FDwfAnalogInBufferSizeGet(self.hdwf, ctypes.byref(ai_buffer))
        self.dwf.FDwfAnalogInChannelRangeGet(self.hdwf, inp, ctypes.byref(ai_range))
        if self.reference_channel is not None:
            self.dwf.FDwfAnalogInChannelRangeGet(
                self.hdwf, ctypes.c_int(self.reference_channel), ctypes.byref(ref_range)
            )
        self.dwf.FDwfAnalogInTriggerSourceGet(self.hdwf, ctypes.byref(trigger_source))
        return {
            "ao_freq_hz": float(ao_freq.value),
            "ao_amp_v": float(ao_amp.value),
            "reference_output_channel": float(self.reference_output_channel) if self.reference_output_channel is not None else float("nan"),
            "reference_ao_freq_hz": float(ref_ao_freq.value),
            "reference_ao_amp_v": float(ref_ao_amp.value),
            "ai_freq_hz": float(ai_freq.value),
            "ai_buffer": float(ai_buffer.value),
            "ai_range_v": float(ai_range.value),
            "reference_channel": float(self.reference_channel) if self.reference_channel is not None else float("nan"),
            "reference_range_v": float(ref_range.value),
            "trigger_source": float(trigger_source.value),
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

    def _capture_one(self) -> tuple[np.ndarray, np.ndarray | None]:
        out_all = ctypes.c_int(-1 if self.reference_output_channel is not None else self.output_channel)
        inp = ctypes.c_int(self.input_channel)
        # Stop then arm then start: this fixes the capture phase relative to W1/W2.
        self.dwf.FDwfAnalogOutConfigure(self.hdwf, out_all, ctypes.c_int(0))
        time.sleep(0.001)
        if self.dwf.FDwfAnalogInConfigure(self.hdwf, ctypes.c_int(1), ctypes.c_int(1)) != 1:
            raise RuntimeError(f"FDwfAnalogInConfigure failed: {self._last_error()}")
        if self.dwf.FDwfAnalogOutConfigure(self.hdwf, out_all, ctypes.c_int(1)) != 1:
            raise RuntimeError(f"FDwfAnalogOutConfigure failed: {self._last_error()}")

        status = ctypes.c_byte(0)
        started = time.monotonic()
        while True:
            if self.dwf.FDwfAnalogInStatus(self.hdwf, ctypes.c_int(1), ctypes.byref(status)) != 1:
                raise RuntimeError(f"FDwfAnalogInStatus failed: {self._last_error()}")
            if status.value == DWF_STATE_DONE:
                break
            if time.monotonic() - started > self.timeout_s:
                raise TimeoutError("AD3 triggered lock-in capture timed out")
            time.sleep(0.0005)

        self.dwf.FDwfAnalogInStatusData(
            self.hdwf, inp, self._buffer, ctypes.c_int(self.buffer_size)
        )
        signal = np.ctypeslib.as_array(self._buffer).copy()
        reference = None
        if self.reference_channel is not None:
            self.dwf.FDwfAnalogInStatusData(
                self.hdwf,
                ctypes.c_int(self.reference_channel),
                self._reference_buffer,
                ctypes.c_int(self.buffer_size),
            )
            reference = np.ctypeslib.as_array(self._reference_buffer).copy()
        return signal, reference

    def measure(self) -> tuple[TriggeredLockInMeasurement, np.ndarray, np.ndarray, np.ndarray]:
        captures = []
        raw_xs = []
        raw_ys = []
        signal_raw_xs = []
        signal_raw_ys = []
        reference_raw_xs = []
        reference_raw_ys = []
        vpps = []
        means = []
        stds = []
        drive_amps = []
        drive_phases = []
        peak_freqs = []
        peak_amps = []
        lockin_window_counts = []
        lockin_window_kepts = []
        lockin_window_rejects = []
        lockin_window_phase_stds = []
        lockin_window_mag_rel_stds = []
        spectra = []
        amps = []

        for _ in range(max(1, self.averages)):
            waveform_i, reference_i = self._capture_one()
            self._check_clipping(waveform_i, self.input_channel, "signal")
            if reference_i is not None:
                self._check_clipping(reference_i, self.reference_channel, "reference")
            captures.append(waveform_i)
            stable_i = waveform_i[self._demod_slice]
            centered_i = stable_i - float(np.mean(stable_i))
            lockin_i = self._demodulate_lockin_windows(stable_i)
            signal_complex = complex(float(lockin_i["raw_x"]), float(lockin_i["raw_y"]))
            signal_raw_xs.append(float(signal_complex.real))
            signal_raw_ys.append(float(signal_complex.imag))
            if reference_i is not None:
                reference_complex = self._demodulate_complex(reference_i[self._demod_slice])
                reference_raw_xs.append(float(reference_complex.real))
                reference_raw_ys.append(float(reference_complex.imag))
                if abs(reference_complex) <= 1e-15:
                    raise RuntimeError("reference channel lock-in magnitude is zero")
                measured_complex = signal_complex / reference_complex
            else:
                measured_complex = signal_complex
                reference_raw_xs.append(float("nan"))
                reference_raw_ys.append(float("nan"))
            raw_xs.append(float(measured_complex.real))
            raw_ys.append(float(measured_complex.imag))
            lockin_window_counts.append(float(lockin_i["window_count"]))
            lockin_window_kepts.append(float(lockin_i["window_kept"]))
            lockin_window_rejects.append(float(lockin_i["window_rejected"]))
            lockin_window_phase_stds.append(float(lockin_i["window_phase_std_deg"]))
            lockin_window_mag_rel_stds.append(float(lockin_i["window_magnitude_rel_std"]))
            vpps.append(float(np.percentile(stable_i, 95) - np.percentile(stable_i, 5)))
            means.append(float(np.mean(stable_i)))
            stds.append(float(np.std(stable_i, ddof=1)))

            spectrum_i = np.fft.rfft(centered_i * self._window)
            coherent_gain = float(np.sum(self._window) / self.buffer_size)
            amp_i = (2.0 / (self.buffer_size * coherent_gain)) * np.abs(spectrum_i)
            phase_i = np.angle(spectrum_i, deg=True)
            search = self._freqs >= 100.0
            peak_idx_rel = int(np.argmax(amp_i[search]))
            peak_idx = int(np.flatnonzero(search)[peak_idx_rel])
            drive_amps.append(float(amp_i[self._drive_idx]))
            drive_phases.append(float(phase_i[self._drive_idx]))
            peak_freqs.append(float(self._freqs[peak_idx]))
            peak_amps.append(float(amp_i[peak_idx]))
            spectra.append(spectrum_i)
            amps.append(amp_i)

        # Aggregate lock-in coordinates after demodulation. Averaging waveforms
        # first can attenuate the result when repeated triggered captures have
        # small phase offsets.
        raw_x = float(np.median(raw_xs))
        raw_y = float(np.median(raw_ys))
        signal_raw_x = float(np.median(signal_raw_xs))
        signal_raw_y = float(np.median(signal_raw_ys))
        reference_raw_x = float(np.nanmedian(reference_raw_xs))
        reference_raw_y = float(np.nanmedian(reference_raw_ys))
        waveform = np.mean(np.vstack(captures), axis=0)
        magnitude = float(math.hypot(raw_x, raw_y))
        phase_deg = float(math.degrees(math.atan2(raw_y, raw_x)))
        signal_magnitude = float(math.hypot(signal_raw_x, signal_raw_y))
        signal_phase_deg = float(math.degrees(math.atan2(signal_raw_y, signal_raw_x)))
        reference_magnitude = float(math.hypot(reference_raw_x, reference_raw_y))
        reference_phase_deg = float(math.degrees(math.atan2(reference_raw_y, reference_raw_x)))
        amp = np.median(np.vstack(amps), axis=0)
        drive_amp = float(np.median(drive_amps))
        peak_amp = float(np.median(peak_amps))

        measurement = TriggeredLockInMeasurement(
            raw_x=raw_x,
            raw_y=raw_y,
            magnitude=magnitude,
            phase_deg=phase_deg,
            signal_raw_x=signal_raw_x,
            signal_raw_y=signal_raw_y,
            signal_magnitude=signal_magnitude,
            signal_phase_deg=signal_phase_deg,
            reference_raw_x=reference_raw_x,
            reference_raw_y=reference_raw_y,
            reference_magnitude=reference_magnitude,
            reference_phase_deg=reference_phase_deg,
            vpp=float(np.median(vpps)),
            mean_v=float(np.median(means)),
            std_v=float(np.median(stds)),
            drive_bin_amp=drive_amp,
            drive_bin_phase_deg=float(np.median(drive_phases)),
            peak_freq_hz=float(np.median(peak_freqs)),
            peak_amp=peak_amp,
            snr_drive_to_peak=float(drive_amp / peak_amp) if peak_amp > 0 else float("nan"),
            sample_rate_hz=self.sample_rate_hz,
            buffer_size=self.buffer_size,
            lockin_window_count=int(np.median(lockin_window_counts)),
            lockin_window_kept=int(np.median(lockin_window_kepts)),
            lockin_window_rejected=int(np.median(lockin_window_rejects)),
            lockin_window_phase_std_deg=float(np.nanmedian(lockin_window_phase_stds)),
            lockin_window_magnitude_rel_std=float(np.nanmedian(lockin_window_mag_rel_stds)),
        )
        return measurement, waveform, self._freqs, amp

    def close(self) -> None:
        if self.hdwf.value != HDWF_NONE:
            out = -1 if self.reference_output_channel is not None else self.output_channel
            self.dwf.FDwfAnalogOutConfigure(
                self.hdwf, ctypes.c_int(out), ctypes.c_int(0)
            )
            self.dwf.FDwfAnalogOutReset(self.hdwf, ctypes.c_int(-1))
            self.dwf.FDwfDeviceClose(self.hdwf)
            self.hdwf = ctypes.c_int(HDWF_NONE)

    def __enter__(self) -> "AD3TriggeredLockIn":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
