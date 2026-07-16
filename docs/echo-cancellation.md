# Echo cancellation — dictating while audio plays

## Problem

voice-typed records from the default PipeWire source (your mic). Recording is
never *blocked* by other audio — PipeWire mics are shareable — but when sound
plays through **speakers** (a Teams call, a YouTube video), the mic hears it
acoustically and the bleed lands in your transcript as stray words or lower
accuracy. Headphones avoid this entirely; for speaker use, enable system-level
echo cancellation.

## Fix — PipeWire WebRTC AEC virtual source

No app change needed: `pw-record` follows the default source, so pointing the
default at an echo-cancelled virtual source fixes every recording.

### 1. Check the AEC plugin exists

```bash
ls /usr/lib/*/spa-0.2/aec/libspa-aec-webrtc.so   # ships with pipewire on Ubuntu
```

### 2. Create the module config

`~/.config/pipewire/pipewire.conf.d/99-echo-cancel.conf`:

```
# Echo-cancelled mic source (WebRTC AEC).
# monitor.mode: uses the default sink's monitor as the far-end reference,
# so no virtual sink / app rerouting is needed — whatever plays through the
# speakers is subtracted from the mic capture.
context.modules = [
    { name = libpipewire-module-echo-cancel
        args = {
            library.name = aec/libspa-aec-webrtc
            monitor.mode = true
            source.props = {
                node.name        = "echo-cancel-source"
                node.description = "Echo-Cancelled Microphone"
            }
            aec.args = {
                webrtc.gain_control    = true
                webrtc.extended_filter = true
            }
        }
    }
]
```

### 3. Restart PipeWire and set the default source

```bash
systemctl --user restart pipewire pipewire-pulse wireplumber
pactl set-default-source echo-cancel-source
```

WirePlumber persists the default across reboots
(`~/.local/state/wireplumber/default-nodes`); the conf.d file reloads the
module at every start. `voice-typed doctor` reports whether the
echo-cancelled source is active.

## Verify

Play something loud through the speakers and record a few seconds:

```bash
pw-record --format s16 --rate 16000 --channels 1 /tmp/ec-test.wav  # Ctrl-C after ~5s
ffmpeg -i /tmp/ec-test.wav -af volumedetect -f null /dev/null
```

Measured on the reference machine (440 Hz tone at 100% sink volume):

| Condition                | mean dB | max dB |
|--------------------------|---------|--------|
| quiet room, no AEC       | -25.7   | -8.3   |
| tone playing, no AEC     | -15.6   | -0.4   |
| tone playing, **AEC on** | -54.0   | -35.3  |

~38 dB of suppression — the bleed drops below the quiet-room noise floor.

## Notes

- Other apps (Teams, browser) inherit the echo-cancelled source too; double
  AEC is harmless.
- If the mic ever goes silent, suspect the EC module:
  `systemctl --user restart pipewire wireplumber` (WirePlumber falls back to
  the hardware mic if the EC node is gone — dictation keeps working, bleed
  returns).
