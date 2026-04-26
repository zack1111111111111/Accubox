"""
TENG Real-Time DAQ + Punch Detection + Interactive AI Coach
============================================================
功能:
- 实时 punch 检测 + 解说（自动）
- 按住空格键说话 → Whisper STT → GPT 回答 → ElevenLabs TTS
- 智能教练模式：长时间没动 / 连击 / 重击 等情况主动评论

依赖:
    pip install pyserial matplotlib numpy elevenlabs pygame python-dotenv
    pip install openai sounddevice scipy keyboard

.env 文件:
    ELEVENLABS_API_KEY=sk_xxx
    OPENAI_API_KEY=sk-proj-xxx

操作:
    程序启动后:
    - 自动模式: 打 TENG，听到自动解说
    - 对话模式: 按住空格键说话, 松开后等回答
    - 关闭窗口或 Ctrl+C 保存 CSV
"""

import os
import sys
import time
import csv
import threading
import queue
import hashlib
import argparse
import random
import io
import wave
from datetime import datetime
from collections import deque
from pathlib import Path

import serial
import serial.tools.list_ports
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import numpy as np

# 可选依赖检查
try:
    from elevenlabs.client import ElevenLabs
    from dotenv import load_dotenv
    import pygame
    HAS_TTS = True
except ImportError as e:
    print(f"[WARN] TTS dependencies missing: {e}")
    HAS_TTS = False

try:
    from openai import OpenAI
    import sounddevice as sd
    from scipy.io import wavfile
    HAS_VOICE = True
except ImportError as e:
    print(f"[WARN] Voice dependencies missing: {e}")
    print("       Run: pip install openai sounddevice scipy")
    HAS_VOICE = False

try:
    import keyboard  # 全局键盘监听
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False
    print("[WARN] 'keyboard' package missing. Push-to-talk disabled.")
    print("       Run: pip install keyboard")

# ======== ADC / 显示配置 ========
DEFAULT_BAUD = 115200
ADC_MAX = 4095
V_REF = 3.3
V_BIAS = 1.65
DISPLAY_WINDOW = 2000
PLOT_INTERVAL_MS = 30

# ======== Punch 检测参数 ========
PEAK_THRESHOLD = 1.1
MIN_RISE = 0.3
COOLDOWN = 0.2
BASELINE_MAX = 0.95
PRE_MIN_THRESHOLD = 0.8
PRE_WINDOW = 0.05
BASELINE_WINDOW = 0.05

# ======== TTS 配置 ========
ELEVEN_VOICE = "nPczCjzI2devNBz1zQrb"  # Brian
ELEVEN_MODEL = "eleven_flash_v2_5"
TTS_CACHE_DIR = "tts_cache"
HIGH_PEAK_THRESHOLD = 1.35
COMBO_WINDOW = 1.5
COMBO_MIN = 3

# ======== 对话配置 ========
WHISPER_MODEL = "whisper-1"
GPT_MODEL = "gpt-4o-mini"
RECORDING_SAMPLE_RATE = 16000
RECORDING_CHANNELS = 1
PUSH_TO_TALK_KEY = "space"
MIN_RECORDING_SECONDS = 0.3  # 太短的录音忽略

# ======== 智能教练参数 ========
IDLE_PROMPT_AFTER_S = 15.0  # 空闲多久后主动鼓励
COACH_COMMENT_INTERVAL = 20.0  # 主动评论的最短间隔
# ====================


def adc_to_voltage(raw):
    return (raw / ADC_MAX) * V_REF


def adc_to_signal_voltage(raw):
    return (raw / ADC_MAX) * V_REF - V_BIAS


def list_serial_ports():
    return [(p.device, p.description) for p in serial.tools.list_ports.comports()]


def auto_detect_port():
    ports = list_serial_ports()
    if not ports:
        return None
    keywords = ['CP210', 'CH340', 'CH910', 'USB', 'UART', 'Silicon']
    for d, dsc in ports:
        for kw in keywords:
            if kw.lower() in dsc.lower():
                return d
    return ports[0][0]


# ============ 解说模板（自动模式用，缓存友好）============

NUMBER_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five",
                6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten"}
BIG_HIT_PHRASES = ["Big one!", "Huge hit!", "Boom!", "Massive!",
                   "Crushing blow!", "Power shot!", "What a hit!"]
COMBO_PHRASES = ["Combo!", "Three in a row!", "Combination!",
                 "Quick hands!", "He's on fire!", "Lighting it up!"]
MILESTONE_PHRASES = {
    5: "Five punches!", 10: "Ten punches! Great pace!",
    15: "Fifteen! Keep it up!", 20: "Twenty! Outstanding!",
    25: "Twenty-five and counting!", 30: "Thirty! Incredible!",
    50: "Fifty punches! Beast mode!", 100: "One hundred! Legendary!"
}


def pick_auto_commentary(count, peak, recent_punches, last_time, now):
    if count in MILESTONE_PHRASES:
        return MILESTONE_PHRASES[count], 0
    if peak > HIGH_PEAK_THRESHOLD and now - last_time > 2.0:
        return random.choice(BIG_HIT_PHRASES), 1
    if len(recent_punches) >= COMBO_MIN and now - last_time > 1.5:
        return random.choice(COMBO_PHRASES), 1
    if count <= 10 and now - last_time > 1.0 and count in NUMBER_WORDS:
        return NUMBER_WORDS[count] + "!", 2
    if count > 10 and count % 5 == 0 and now - last_time > 1.5:
        return f"{count} punches!", 2
    return None


# ============ TTS Worker ============

class TTSWorker(threading.Thread):
    def __init__(self, api_key, voice=ELEVEN_VOICE, cache_dir=TTS_CACHE_DIR):
        super().__init__(daemon=True)
        self.api_key = api_key
        self.voice = voice
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        # 用 PriorityQueue 实现优先级播放
        self.queue = queue.PriorityQueue()
        self.stop_flag = threading.Event()
        self.client = None
        self.api_calls = 0
        self.cache_hits = 0
        self.errors = 0
        self.is_speaking = False
        self.interrupt_flag = threading.Event()
        self._counter = 0  # 用于 PriorityQueue tie-break

        if api_key:
            try:
                self.client = ElevenLabs(api_key=api_key)
                print(f"[TTS] ElevenLabs client OK (voice='{voice}')")
            except Exception as e:
                print(f"[TTS ERROR] init: {e}")

        try:
            pygame.mixer.init(frequency=44100)
        except Exception as e:
            print(f"[TTS ERROR] mixer init: {e}")

    def say(self, text, priority=2, interrupt=False):
        if not text:
            return
        if interrupt:
            # 高优先级 + 打断当前播放
            self.interrupt_flag.set()
        self._counter += 1
        # PriorityQueue: 数字越小越优先
        self.queue.put((priority, self._counter, text))

    def clear_low_priority(self):
        """清空优先级 1+ 的项目（保留 priority 0）"""
        new_q = queue.PriorityQueue()
        while not self.queue.empty():
            try:
                p, c, t = self.queue.get_nowait()
                if p == 0:
                    new_q.put((p, c, t))
            except queue.Empty:
                break
        self.queue = new_q

    def _cache_path(self, text):
        h = hashlib.md5(f"{self.voice}::{text}".encode()).hexdigest()[:16]
        safe = "".join(c if c.isalnum() else "_" for c in text)[:30]
        return self.cache_dir / f"{safe}_{h}.mp3"

    def _generate(self, text):
        if not self.client:
            return None
        try:
            audio_iter = self.client.text_to_speech.convert(
                voice_id=self.voice, text=text,
                model_id=ELEVEN_MODEL, output_format="mp3_44100_64",
            )
            self.api_calls += 1
            return b"".join(audio_iter)
        except Exception as e:
            print(f"[TTS ERROR] '{text}': {e}")
            self.errors += 1
            return None

    def _play(self, path):
        try:
            # 关键：开始新播放前清空 interrupt flag
            # 否则上一次设置的 flag 会立刻打断这次的播放
            self.interrupt_flag.clear()
            self.is_speaking = True
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if self.stop_flag.is_set() or self.interrupt_flag.is_set():
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.05)
        except Exception as e:
            print(f"[TTS ERROR] play: {e}")
            self.errors += 1
        finally:
            self.is_speaking = False
            self.interrupt_flag.clear()

    def run(self):
        while not self.stop_flag.is_set():
            try:
                priority, _, text = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            cache = self._cache_path(text)
            if cache.exists():
                self.cache_hits += 1
                self._play(cache)
            else:
                audio = self._generate(text)
                if audio:
                    cache.write_bytes(audio)
                    self._play(cache)

    def stop(self):
        self.stop_flag.set()


# ============ Punch Detector ============

class PunchDetector:
    def __init__(self):
        max_w = max(PRE_WINDOW, BASELINE_WINDOW) * 1.5
        self.buffer = deque(maxlen=int(max_w * 10000) + 100)
        self.last_punch_time = -999.0
        self.punch_count = 0
        self.punch_times = []
        self.punch_peaks = []

    def update(self, t, v):
        self.buffer.append((t, v))
        if len(self.buffer) < 2 or t - self.buffer[0][0] < PRE_WINDOW:
            return False, None
        ts = np.array([x[0] for x in self.buffer])
        vs = np.array([x[1] for x in self.buffer])
        baseline = vs[ts >= (t - BASELINE_WINDOW)].mean()
        rise = v - baseline
        pre_mask = (ts >= t - PRE_WINDOW) & (ts < t)
        if not pre_mask.any():
            return False, None
        pre_avg = vs[pre_mask].mean()
        pre_min = vs[pre_mask].min()
        if (v >= PEAK_THRESHOLD and rise >= MIN_RISE and
                pre_avg < BASELINE_MAX and pre_min < PRE_MIN_THRESHOLD and
                t - self.last_punch_time > COOLDOWN):
            self.last_punch_time = t
            self.punch_count += 1
            self.punch_times.append(t)
            self.punch_peaks.append(v)
            return True, {'time': t, 'peak': v, 'rise': rise,
                          'pre_min': pre_min, 'count': self.punch_count}
        return False, None


# ============ 对话引擎（Whisper + GPT）============

class ConversationEngine(threading.Thread):
    """
    后台线程: 监听空格键, 录音, 调 Whisper + GPT, 把回答送进 TTS 队列。
    持有对 TTS worker 和 detector 的引用，以便看到当前训练状态。
    """

    SYSTEM_PROMPT = """You are an enthusiastic boxing coach AI assistant for a TENG-based punch tracking system called AccuBox. You give SHORT (1-2 sentences max), energetic, motivational responses. Use the live training stats given to you to make responses specific. Examples:
- "How am I doing?" → "23 punches in 45 seconds, that's a solid pace! Keep that rhythm going!"
- "Tell me my power" → "Your average punch is at 1.18 volts—solid! Try to push past 1.3 for power shots."
- "Stop talking" → "Got it, going quiet."
Keep it punchy. No lectures. Reply with just the spoken text, no formatting."""

    def __init__(self, openai_key, tts_worker, daq):
        super().__init__(daemon=True)
        self.tts = tts_worker
        self.daq = daq  # 引用 DAQ 拿到 detector 和 timestamps
        self.stop_flag = threading.Event()
        self.client = None
        self.recording_q = queue.Queue()  # 录音数据片段
        self.is_recording = False
        self.recording_buffer = []
        self.api_calls = 0
        self.transcripts = []  # (time, user_text, ai_text)

        if openai_key:
            try:
                self.client = OpenAI(api_key=openai_key)
                print("[Voice] OpenAI client OK")
            except Exception as e:
                print(f"[Voice ERROR] init: {e}")

    def _get_training_context(self):
        """生成给 GPT 的训练状态上下文"""
        d = self.daq.detector
        elapsed = (self.daq.timestamps[-1] - self.daq.timestamps[0]
                   if len(self.daq.timestamps) > 1 else 0)
        if d.punch_count == 0:
            return f"Training session: {elapsed:.0f}s elapsed, no punches yet."
        avg_peak = float(np.mean(d.punch_peaks)) if d.punch_peaks else 0
        max_peak = float(np.max(d.punch_peaks)) if d.punch_peaks else 0
        # 最近 30 秒
        now = self.daq.timestamps[-1] if self.daq.timestamps else 0
        recent_30 = [pt for pt in d.punch_times if pt >= now - 30]
        rate = len(recent_30) / min(30, max(now, 1)) * 60 if now > 0 else 0
        time_since_last = now - d.last_punch_time if d.last_punch_time > 0 else 0
        return (
            f"Live training stats:\n"
            f"- Total punches: {d.punch_count}\n"
            f"- Session time: {elapsed:.1f} seconds\n"
            f"- Recent rate: {rate:.0f} punches/min (last 30s)\n"
            f"- Average punch power: {avg_peak:.2f}V\n"
            f"- Peak power: {max_peak:.2f}V\n"
            f"- Time since last punch: {time_since_last:.1f}s"
        )

    def _start_recording(self):
        """开始录音"""
        if self.is_recording:
            return
        self.is_recording = True
        self.recording_buffer = []
        # 打断当前 TTS（如果在播）
        if self.tts.is_speaking:
            self.tts.interrupt_flag.set()
        # 清掉所有低优先级队列项
        self.tts.clear_low_priority()
        print("[Voice] 🎤 Recording...")

        def callback(indata, frames, t_info, status):
            if self.is_recording:
                self.recording_buffer.append(indata.copy())

        try:
            self.input_stream = sd.InputStream(
                samplerate=RECORDING_SAMPLE_RATE,
                channels=RECORDING_CHANNELS,
                dtype='int16',
                callback=callback,
            )
            self.input_stream.start()
            self.recording_start_time = time.time()
        except Exception as e:
            print(f"[Voice ERROR] recording start: {e}")
            self.is_recording = False

    def _stop_recording_and_process(self):
        """停止录音 → Whisper → GPT → TTS"""
        if not self.is_recording:
            return
        self.is_recording = False
        try:
            self.input_stream.stop()
            self.input_stream.close()
        except Exception:
            pass

        duration = time.time() - self.recording_start_time
        if duration < MIN_RECORDING_SECONDS:
            print(f"[Voice] Too short ({duration:.2f}s), ignored.")
            return

        if not self.recording_buffer:
            print("[Voice] Empty buffer.")
            return

        print(f"[Voice] Processing ({duration:.1f}s)...")

        # 把所有片段合并成一个 wav 文件 (内存中)
        audio_data = np.concatenate(self.recording_buffer, axis=0)
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(RECORDING_CHANNELS)
            wf.setsampwidth(2)  # int16
            wf.setframerate(RECORDING_SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        wav_buffer.seek(0)
        wav_buffer.name = "audio.wav"

        # Whisper STT
        try:
            transcript = self.client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=wav_buffer,
                language="en",
            )
            user_text = transcript.text.strip()
            self.api_calls += 1
        except Exception as e:
            print(f"[Voice ERROR] Whisper: {e}")
            return

        if not user_text:
            print("[Voice] No speech detected.")
            return

        print(f"[Voice] You said: {user_text!r}")

        # GPT 生成回答
        context = self._get_training_context()
        try:
            completion = self.client.chat.completions.create(
                model=GPT_MODEL,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "system", "content": context},
                    {"role": "user", "content": user_text},
                ],
                max_tokens=80,
                temperature=0.7,
            )
            ai_text = completion.choices[0].message.content.strip()
            self.api_calls += 1
        except Exception as e:
            print(f"[Voice ERROR] GPT: {e}")
            return

        print(f"[Coach] {ai_text}")
        self.transcripts.append((time.time(), user_text, ai_text))
        # 高优先级 TTS（打断当前低优先级播放）
        self.tts.say(ai_text, priority=0, interrupt=True)

    def run(self):
        if not HAS_KEYBOARD or not self.client:
            print("[Voice] Conversation disabled (keyboard or OpenAI missing).")
            return

        print(f"[Voice] Ready! Press and hold {PUSH_TO_TALK_KEY.upper()} to talk.")

        # 用 keyboard 库注册全局热键
        keyboard.on_press_key(PUSH_TO_TALK_KEY, lambda _: self._start_recording())
        keyboard.on_release_key(PUSH_TO_TALK_KEY,
                                lambda _: self._stop_recording_and_process())

        while not self.stop_flag.is_set():
            time.sleep(0.1)

    def stop(self):
        self.stop_flag.set()


# ============ DAQ (主类) ============

class TENGDAQ:
    def __init__(self, port, baud=DEFAULT_BAUD,
                 enable_tts=True, enable_voice=True,
                 eleven_key=None, openai_key=None):
        self.port = port
        self.baud = baud

        self.timestamps = []
        self.raw_values = []
        self.voltages = []
        self.window_t = deque(maxlen=DISPLAY_WINDOW)
        self.window_v = deque(maxlen=DISPLAY_WINDOW)

        self.t_start = None
        self.ser = None
        self.fig = None
        self.ax = None
        self.line = None
        self.punch_markers = None
        self.text_info = None
        self.text_punch = None
        self.text_commentary = None

        self.detector = PunchDetector()
        self.window_punches_t = deque(maxlen=DISPLAY_WINDOW)
        self.window_punches_v = deque(maxlen=DISPLAY_WINDOW)

        # TTS
        self.tts = None
        self.last_commentary_time = 0
        self.last_commentary_text = ""
        if enable_tts and HAS_TTS and eleven_key:
            self.tts = TTSWorker(api_key=eleven_key)
            self.tts.start()

        # 对话引擎
        self.voice = None
        if (enable_voice and HAS_VOICE and HAS_KEYBOARD and
                openai_key and self.tts):
            self.voice = ConversationEngine(openai_key, self.tts, self)
            self.voice.start()

        # 智能教练
        self.last_coach_time = 0
        self.idle_warned = False

        # 启动语
        if self.tts:
            self.tts.say("Welcome to AccuBox! Press space to talk to me, "
                         "or just start punching!", priority=0)

    def connect(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
            time.sleep(2)
            self.ser.reset_input_buffer()
            print(f"[OK] Serial connected: {self.port}")
            return True
        except serial.SerialException as e:
            print(f"[ERROR] Serial: {e}")
            return False

    def read_one(self):
        try:
            line = self.ser.readline().decode('utf-8', errors='ignore').strip()
            if not line or line.startswith('#'):
                return None
            raw = int(line)
            if 0 <= raw <= ADC_MAX:
                return raw, adc_to_voltage(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        return None

    def setup_plot(self):
        plt.style.use('default')
        self.fig, self.ax = plt.subplots(figsize=(13, 7))
        self.fig.canvas.manager.set_window_title('AccuBox - AI Coach')

        self.line, = self.ax.plot([], [], lw=1.0, color='#0077b6', label='Signal')
        self.punch_markers, = self.ax.plot(
            [], [], 'rx', markersize=14, markeredgewidth=2.5, label='Punch')
        self.ax.axhline(y=PEAK_THRESHOLD, color='red', linestyle='--',
                        alpha=0.4, lw=1, label=f'Peak ({PEAK_THRESHOLD}V)')
        self.ax.axhline(y=HIGH_PEAK_THRESHOLD, color='purple', linestyle='--',
                        alpha=0.4, lw=1, label=f'Big hit ({HIGH_PEAK_THRESHOLD}V)')
        self.ax.set_ylim(0, V_REF)
        self.ax.set_ylabel('ADC Voltage (V)')
        self.ax.set_xlabel('Time (s)')
        self.ax.set_title('AccuBox: Hold SPACE to talk to coach — close to save CSV')
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc='upper right', fontsize=9)

        self.text_info = self.ax.text(
            0.02, 0.97, '', transform=self.ax.transAxes,
            fontsize=9, verticalalignment='top', family='monospace',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.85))
        self.text_punch = self.ax.text(
            0.98, 0.05, 'PUNCHES\n0', transform=self.ax.transAxes,
            fontsize=24, fontweight='bold', color='#d00000',
            ha='right', va='bottom',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#fff5f5',
                      edgecolor='#d00000', linewidth=2))
        self.text_commentary = self.ax.text(
            0.5, 0.92, '', transform=self.ax.transAxes,
            fontsize=14, fontweight='bold', color='#0a3a5e',
            ha='center', va='top', wrap=True,
            bbox=dict(boxstyle='round,pad=0.4', facecolor='#fff8d4',
                      edgecolor='#d4a000', linewidth=2, alpha=0.0))

        self.fig.canvas.mpl_connect('close_event', self._on_close)

    def _on_close(self, event):
        self._stop_requested = True

    def _maybe_auto_speak(self, info):
        """普通报数 / 重击 / 连击 / 里程碑（B 模式自动）"""
        if not self.tts:
            return
        # 如果用户在说话或者高优先级在播，不报数
        if self.voice and self.voice.is_recording:
            return
        now = time.time()
        recent = [pt for pt in self.detector.punch_times
                  if info['time'] - pt <= COMBO_WINDOW]
        result = pick_auto_commentary(
            count=info['count'], peak=info['peak'],
            recent_punches=recent,
            last_time=self.last_commentary_time, now=now)
        if result is None:
            return
        text, prio = result
        self.tts.say(text, priority=prio)
        self.last_commentary_time = now
        self.last_commentary_text = text
        print(f"[COMMENT] {text!r}")

    def _maybe_coach_remark(self, t_now):
        """智能教练: 长时间没动 / 训练时间长 等触发主动评论"""
        if not self.tts:
            return
        if self.voice and self.voice.is_recording:
            return
        if t_now - self.last_coach_time < COACH_COMMENT_INTERVAL:
            return

        # 距离上次 punch 超过 IDLE_PROMPT_AFTER_S
        if (self.detector.punch_count > 0 and
                t_now - self.detector.last_punch_time > IDLE_PROMPT_AFTER_S and
                not self.idle_warned):
            phrases = [
                "Hey, taking a break? Let's get back to it!",
                "Don't stop now! Keep going!",
                "Come on, let's see some more punches!",
            ]
            self.tts.say(random.choice(phrases), priority=1)
            self.last_coach_time = t_now
            self.idle_warned = True
            return

        # 重新打了 → 重置 idle_warned
        if t_now - self.detector.last_punch_time < 2:
            self.idle_warned = False

    def update_plot(self, frame):
        if self.t_start is None:
            self.t_start = time.time()

        read_count = 0
        while self.ser.in_waiting and read_count < 500:
            r = self.read_one()
            if r is None:
                break
            raw, v = r
            t_now = time.time() - self.t_start
            self.timestamps.append(t_now)
            self.raw_values.append(raw)
            self.voltages.append(v)
            self.window_t.append(t_now)
            self.window_v.append(v)

            is_p, info = self.detector.update(t_now, v)
            if is_p:
                self.window_punches_t.append(info['time'])
                self.window_punches_v.append(info['peak'])
                print(f"PUNCH #{info['count']:3d} @ {info['time']:7.3f}s | "
                      f"peak: {info['peak']:.3f}V")
                self._maybe_auto_speak(info)
            read_count += 1

        # 智能教练（每帧检查一次）
        if self.timestamps:
            self._maybe_coach_remark(self.timestamps[-1])

        # 清理窗口外的 punch 标记
        if self.window_t:
            t_old = self.window_t[0]
            while self.window_punches_t and self.window_punches_t[0] < t_old:
                self.window_punches_t.popleft()
                self.window_punches_v.popleft()

        if len(self.window_t) > 1:
            self.line.set_data(self.window_t, self.window_v)
            self.ax.set_xlim(self.window_t[0], self.window_t[-1] + 0.01)
            self.punch_markers.set_data(list(self.window_punches_t),
                                        list(self.window_punches_v))

            v_arr = np.array(self.window_v)
            stats = (
                f"Samples: {len(self.voltages)}\n"
                f"Time:    {self.window_t[-1]:.1f}s\n"
                f"Peak:    {v_arr.max():.3f}V\n"
            )
            if len(self.timestamps) > 10:
                dt = self.timestamps[-1] - self.timestamps[-min(100, len(self.timestamps))]
                n = min(100, len(self.timestamps)) - 1
                if dt > 0:
                    stats += f"Rate:    ~{n/dt:.0f} Hz\n"
            if self.detector.punch_count > 0:
                t_max = self.window_t[-1]
                recent = [pt for pt in self.detector.punch_times if pt >= t_max - 30]
                if recent:
                    stats += f"Punches: {len(recent)/min(30, t_max)*60:.0f}/min\n"
            if self.tts:
                stats += f"TTS: {self.tts.api_calls} api, {self.tts.cache_hits} cache\n"
            if self.voice:
                rec_status = "🎤 RECORDING" if self.voice.is_recording else "press SPACE"
                stats += f"Voice: {rec_status}"

            self.text_info.set_text(stats)
            self.text_punch.set_text(f'PUNCHES\n{self.detector.punch_count}')

            # 显示最近一次解说（自动 + 教练对话）
            display_text = ""
            display_time = 0
            if self.last_commentary_text:
                display_text = self.last_commentary_text
                display_time = self.last_commentary_time
            if self.voice and self.voice.transcripts:
                last = self.voice.transcripts[-1]
                if last[0] > display_time:
                    display_text = f'You: "{last[1]}"\n🥊 {last[2]}'
                    display_time = last[0]

            if display_text:
                age = time.time() - display_time
                if age < 8:
                    alpha = max(0.1, 1.0 - age / 8)
                    self.text_commentary.set_text(display_text)
                    self.text_commentary.get_bbox_patch().set_alpha(alpha * 0.85)
                else:
                    self.text_commentary.set_text('')
                    self.text_commentary.get_bbox_patch().set_alpha(0)

        return (self.line, self.punch_markers, self.text_info,
                self.text_punch, self.text_commentary)

    def run(self):
        if not self.connect():
            return
        self.setup_plot()
        self._stop_requested = False

        try:
            ani = animation.FuncAnimation(
                self.fig, self.update_plot,
                interval=PLOT_INTERVAL_MS, blit=False, cache_frame_data=False)
            plt.show()
        except KeyboardInterrupt:
            print("\nCtrl+C")
        finally:
            self.cleanup()
            self.save_csv()

    def cleanup(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        if self.tts:
            self.tts.stop()
        if self.voice:
            self.voice.stop()
        print("[OK] Cleaned up.")

    def save_csv(self):
        if not self.timestamps:
            return
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        fn = f'teng_data_{ts}.csv'
        ps = set(round(t, 6) for t in self.detector.punch_times)
        with open(fn, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['time_s', 'adc_raw', 'adc_voltage_V',
                        'signal_voltage_V', 'is_punch'])
            for t, raw, v in zip(self.timestamps, self.raw_values, self.voltages):
                w.writerow([f'{t:.6f}', raw, f'{v:.4f}',
                            f'{adc_to_signal_voltage(raw):.4f}',
                            1 if round(t, 6) in ps else 0])
        print(f"\n[OK] Saved: {fn}")
        print(f"     Punches: {self.detector.punch_count}")
        if self.voice and self.voice.transcripts:
            tfn = f'transcripts_{ts}.txt'
            with open(tfn, 'w', encoding='utf-8') as f:
                for t, u, a in self.voice.transcripts:
                    f.write(f"[{t:.1f}] You: {u}\n")
                    f.write(f"        Coach: {a}\n\n")
            print(f"[OK] Transcripts: {tfn}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', '-p', default=None)
    parser.add_argument('--baud', '-b', type=int, default=DEFAULT_BAUD)
    parser.add_argument('--no-tts', action='store_true')
    parser.add_argument('--no-voice', action='store_true')
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()

    if args.list:
        for d, dsc in list_serial_ports():
            print(f"  {d:20s}  {dsc}")
        return

    load_dotenv()
    eleven_key = os.getenv("ELEVENLABS_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if not eleven_key:
        print("[WARN] ELEVENLABS_API_KEY not in .env")
    if not openai_key:
        print("[WARN] OPENAI_API_KEY not in .env (voice chat disabled)")

    port = args.port or auto_detect_port()
    if not port:
        print("[ERROR] No serial port.")
        sys.exit(1)
    print(f"[INFO] Port: {port}")

    daq = TENGDAQ(
        port=port, baud=args.baud,
        enable_tts=not args.no_tts,
        enable_voice=not args.no_voice,
        eleven_key=eleven_key, openai_key=openai_key,
    )
    daq.run()


if __name__ == '__main__':
    main()
