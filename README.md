Pressure Sensor → Analog Frontend → ESP32 (TX)
                                              ↓
                                        ESP-NOW (2.4 GHz)
                                              ↓
                                        ESP32 (RX) → USB Serial → Laptop
                                                                    ↓
                                                              Python DAQ
                                                                    ↓
                                              ┌─────────────────────┼─────────────────────┐
                                              ↓                     ↓                     ↓
                                       Punch Detector       Auto Commentary        Voice Coach
                                                                  ↓                     ↓
                                                            ElevenLabs            Whisper STT
                                                              (TTS)                    ↓
                                                                                  GPT-4o-mini
                                                                                       ↓
                                                                                  ElevenLabs (TTS)
```
---
Hardware
Component	Notes
Pressure Sensor 	Custom-built impact sensor
LMC6001 op-amp	Ultra-low input current (<25 fA) for high-impedance sensing signal
ESP32 × 2	One wearable (TX), one base station (RX)
12V battery + voltage divider	Powers ±6V split rail for the op-amp (CNI570-style)
1µF capacitor + 10kΩ resistors	AC coupling and 1.65V bias network
Wiring (TX side)
```
Sensor output         → LMC6001 IN-
Battery +6V         → LMC6001 V+
Battery -6V         → LMC6001 V-
Battery GND         → ESP32 GND (must be common ground!)
LMC6001 OUT         → 1µF cap → 10kΩ/10kΩ divider → ESP32 GPIO34
ESP32 3.3V          → top of divider (provides 1.65V bias)
```
---
Software Stack
Python 3.10+ for the DAQ application
Arduino IDE for ESP32 firmware
ElevenLabs API for text-to-speech (Flash v2.5 model, Brian voice)
OpenAI API for Whisper (STT) and GPT-4o-mini (LLM)
matplotlib for real-time visualization
pyserial / pygame / sounddevice / keyboard
---
Getting Started
Prerequisites
```bash
# Python dependencies
pip install -r requirements.txt
# or manually:
pip install pyserial matplotlib numpy elevenlabs pygame python-dotenv openai sounddevice scipy keyboard
```
Setup API keys
Copy `.env.example` to `.env` and fill in your keys:
```
ELEVENLABS_API_KEY=sk_xxxxx
OPENAI_API_KEY=sk-proj-xxxxx
```
Flash the ESP32 boards
Get the receiver's MAC address. Open `01_get_mac_address.ino` in Arduino IDE, flash to the receiver ESP32, open Serial Monitor (115200 baud), copy the MAC.
Flash the receiver. Open `02_esp32_rx_receiver.ino`, flash to the receiver ESP32. This board stays connected to your laptop via USB.
Flash the transmitter. Open `03_esp32_tx_sender.ino`, paste the receiver's MAC into `receiverMac[]`, then flash to the transmitter ESP32. This board connects to the sensor.
Run the system
```bash

# Full version: announcer + AI coach (push spacebar to talk)
python accubox_daq_interactive.py
```
---
File Overview
Python applications (incremental complexity)
File	What it does
`boxing_daq_interactive.py`	Full version with conversational AI coach
ESP32 firmware
File	What it does
`01_get_mac_address.ino`	One-shot utility to print a board's MAC
`02_esp32_rx_receiver.ino`	Receiver: ESP-NOW → USB serial bridge
`03_esp32_tx_sender.ino`	Transmitter: reads ADC, sends via ESP-NOW
`04_esp32_tx_batch_sender.ino`	High-rate batch version of the transmitter
`05_esp32_rx_batch_receiver.ino`	High-rate batch version of the receiver
---
Detection Algorithm
A punch is registered when all of the following are true within a sliding window:
Peak voltage exceeds `PEAK_THRESHOLD` (default 1.1V)
Rise from rolling baseline exceeds `MIN_RISE` (default 0.3V)
Pre-window minimum dipped below `PRE_MIN_THRESHOLD` (default 0.8V) — proves the signal genuinely returned to rest
Pre-window average is below `BASELINE_MAX` (default 0.95V) — proves no noise floor
At least `COOLDOWN` seconds (default 0.2s) since the last detected punch
The pre-window check is the key trick: noise never truly settles, while real punches always have a calm period before impact. This single condition was what separated real punches from sustained sensor noise.
---
Challenges
High-impedance signal noise — The pressure sensor output is sensitive enough to pick up cable wiggle, footsteps, and 60 Hz line interference. Solved with the multi-condition shape-based detector.
Wiring on a moving target — Connections that survive thousands of impacts required iteration on solder joints, strain relief, and grounding.
Brownout under wireless load — Running the TX ESP32 from a 9V battery caused WiFi current spikes to collapse the regulator. Solved by switching to a more robust 5V supply.
ADC scale and bias — Initial signal saturated or clipped against rails. Math-driven design of the AC coupling cap + bias network was the fix.
Voice collisions — Three concurrent TTS sources (counter, callouts, coach) stepped on each other until we built a priority queue with cancellable playback.
---
What's Next
Multi-sensor support (per-glove, body shots) for combo recognition
Force calibration to report Newtons or PSI
Wake-word activation ("Hey Coach") for fully hands-free operation
Session history + personalized AI training programs
Multi-target gym installation with leaderboards
---
License
MIT — see `LICENSE` file.
---
Credits
Built at [Hackathon Name] by [Your Name(s)].
The current preamplifier design is inspired by the open CNI570 project:
> Mallineni et al., *A low-cost approach for measuring electrical load currents in triboelectric nanogenerators*, Nanotechnol Rev 2018.
