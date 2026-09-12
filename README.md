# utter

Local dictation for Wayland. **Alt+Space**, talk, stop talking — the text appears in
whatever you were typing into.

The whole interface is five dots at the bottom of the screen. They appear when you
speak and vanish when you stop. No panel, no window, no buttons.

Nothing leaves your machine. There is no account, no API key, and no subscription.

**Alt+Space, then talk.** When you stop talking it types what you said. No key to
release, no key to press again.

```
utter daemon        # keeps the model resident, arms the trigger
utter keys --watch  # confirm the double-tap is detected
utter toggle        # or drive it from a compositor keybind
```

## Why this exists

Paid dictation apps charge $10–15/month, and the good ones are macOS-only. The
surprise is that the subscription was never buying transcription — transcription is
nearly free now. It buys the *client*: the hotkey, the overlay, the cleanup pass, the
dictionary. That part simply does not exist on Linux, so this is that part.

The other surprise is speed. A resident local model beats the cloud on latency,
because the network round-trip alone costs more than the inference:

| | latency |
|---|---|
| **utter, local, resident model** | **190–250 ms** |
| Aqua Voice (cloud, fastest paid product) | ~450 ms claimed |
| Wispr Flow (cloud) | 700 ms claimed, 1–2 s reported |

## Voice mode

The default flow has no hotkey ceremony at all:

1. **Alt+Space.** Read straight from the kernel's input devices, so it works in any
   application without a compositor keybind, and inserts no character of its own.
2. **Talk.** Five dots at the bottom edge rise and fall with your voice, so you can
   see it is hearing you, and fade out whenever you are quiet. They turn amber while
   transcribing. That is the entire UI.
3. **Stop talking.** After 1.5 s of silence it inserts the text on its own.

Alt+Space again to commit early. `utter cancel` throws the recording away.

### Other triggers

`mode = "double_tap"` taps one key twice — by default space. Double-spacing is
something people genuinely type, so those taps are ignored if any other key was pressed
in the previous 500 ms; pause briefly first and it opens. Its two spaces reach the
window and are deleted afterwards.

### Or hold a key instead

```toml
[trigger]
mode = "hold"
key = "SCROLLLOCK"      # hold to talk, release to insert
hold_ms = 220           # ignore an accidental brush
```

Hold mode is classic push-to-talk: the microphone opens while the key is down and
commits the moment you let go, so silence detection is switched off — pausing
mid-sentence while still holding the key is not the end of your dictation.

**Pick an inert key for this.** The trigger is a passive read, so the key still reaches
your application: holding a key that types something inserts that character over and
over while held, at the compositor's autorepeat rate. Deleting those would mean
guessing that rate, and guessing one too many eats your actual text — so utter does not
try. It warns instead.

| | chord (default) | double-tap | hold |
|---|---|---|---|
| example | `Alt+Space` | `SPACE` twice | `SCROLLLOCK` |
| how it ends | silence, or the chord again | silence, or two more taps | you release |
| stray characters | none | 2 spaces, deleted after | none, on an inert key |
| fires by accident | no | only if you pause then double-space | no |

On a German layout `ALT` means left Alt only: AltGr+Space inserts a non-breaking
space, so binding it would type an invisible character every time. Ask for `ALTGR`
explicitly if you want it anyway.

Two details that make this possible rather than fiddly:

- The trigger is a **passive read, not a grab** — your keystrokes still reach the
  application, which is why the two stray spaces need deleting afterwards. Grabbing the
  keyboard would avoid that but would mean intercepting every keystroke on the system.
- **Synthesised keystrokes are invisible to it.** `wtype` uses the Wayland
  virtual-keyboard protocol rather than creating a kernel device, so utter typing its
  own output can never retrigger itself. No echo suppression needed.

Silence detection looks at a ~380 ms window rather than a single audio chunk. Without
that it cuts people off mid-sentence: the first version committed after 3.4 s because it
mistook a dramatic pause for the end of the sentence.

## Measured

On one machine — Ryzen 7 7700X, Radeon RX 9070 XT (gfx1201), RADV Vulkan,
whisper.cpp 1.9.3, `large-v3-turbo` q8:

| what | measured |
|---|---|
| stop talking → text delivered | **196–246 ms** (11.5 s of audio) |
| model load, per process | 243 ms — which is why the daemon exists |
| 66 s of audio, inference only | 880 ms (≈75× realtime) |
| same model, CPU only (16 threads) | 4.39 s |
| Parakeet 0.6B via `parakeet-cli` | 691–1068 ms (reloads the model each time) |
| optional LLM cleanup pass, warm | 482–924 ms |

GPU returns to idle clocks with VRAM released between utterances.

## Requirements

- A Wayland compositor implementing `zwp_virtual_keyboard_manager_v1` — wlroots-based
  ones do (niri, Sway, Hyprland, river). **GNOME and KDE do not**, so typing will fall
  back to the clipboard there.
- `whisper-cpp`, `wtype`, PipeWire, and a GGML model.
- PyGObject and dbus-python from your distribution. No Python packages to install.

On Arch:

```sh
sudo pacman -S whisper-cpp ggml-vulkan wtype wl-clipboard \
               python-gobject python-dbus gtk4-layer-shell
```

Take `ggml-vulkan`, not `ggml-hip`: on RDNA4, Vulkan measures faster than ROCm, and
the ROCm path has an open bug where the GPU never drops back to idle clocks until the
process exits — bad for something that sits resident all day.

A model, either one:

```sh
mkdir -p ~/ai/stt && cd ~/ai/stt
# fast, resident, the default
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin
# more accurate on English, but reloads every time (~700 ms)
curl -L -o ggml-parakeet-v3-q8.bin \
  https://huggingface.co/ggml-org/parakeet-GGUF/resolve/main/ggml-parakeet-tdt-0.6b-v3-q8_0.bin
```

## Install

```sh
git clone https://github.com/one-more-refactor/utter && cd utter
pip install --user .
utter init          # writes ~/.config/utter/config.toml
utter check         # verifies models, binaries, mic, and the trigger
utter sources       # lists capture devices that actually work as a target
utter keys --watch  # confirm the trigger fires on your keyboard
```

Reading input devices normally needs the `input` group, but systemd-logind usually
grants the active seat's user an ACL on local keyboards, so the trigger often works
with no setup at all. If `utter keys` finds nothing:

```sh
sudo usermod -aG input $USER   # then log out and back in
```

Then run the daemon, and bind a key. For niri, in `binds { }`:

```kdl
Mod+D       { spawn "utter" "toggle"; }
Mod+Shift+D { spawn "utter" "cancel"; }
```

A user service is in `contrib/utter.service`:

```sh
cp contrib/utter.service ~/.config/systemd/user/
systemctl --user enable --now utter
```

## Configuration

Everything is optional; `~/.config/utter/config.toml` only needs what you change.

```toml
[asr]
backend = "whisper-server"          # resident, ~220 ms
model = "~/ai/stt/ggml-large-v3-turbo-q8_0.bin"
language = "en"

[output]
mode = "type"                       # or "clipboard"
two_stage = true

[output.replacements]
"see three do" = "cr3do"            # deterministic, applied before anything else

[cleanup]
enabled = false                     # the optional LLM readability pass
model = "huihui_ai/gemma-4-abliterated:e4b"
intensity = "light"                 # off | light | heavy
vocabulary = ["Authentik", "Proxmox", "niri"]

[trigger]
modifiers = ["ALT"]                 # ALT | CTRL | SHIFT | SUPER | ALTGR
key = "SPACE"
# mode = "double_tap"               # or "hold"

[audio]
auto_stop = true
silence_ms = 1500
silence_level = 0.02

[ui]
overlay = true
tray = true
sounds = true
```

`live_text = true` additionally re-recognises the audio while you speak. It works, and
it is off by default: watching words rewrite themselves is more distracting than the
dots, and it costs a recognition pass every 700 ms.

## The optional cleanup pass

Raw recognition keeps every "um", every false start, and has no paragraphs. A local
LLM pass fixes that. Point `[cleanup]` at any Ollama-compatible endpoint.

Start with it **off**. Plain dictation is ~220 ms; the cleanup pass roughly triples
that, and it introduces an over-editing failure mode where the model "improves" what
you actually said. Turn it on once the fillers start bothering you.

With `two_stage = true` the raw text is typed immediately and replaced when the
cleaned version returns, so a ~1.2 s pipeline still feels like a ~0.2 s one.

Two things worth knowing, both learned the hard way:

- **Use an instruct model, never a reasoning model.** A reasoning model spends its
  whole token budget thinking and returns an empty string. `utter` sends
  `"think": false` and strips `<think>` blocks, but the model choice is still yours.
- **Custom vocabulary belongs here, not in the recogniser.** Feeding a word list to
  Whisper's `initial_prompt` is measurably harmful: around −24% rare-word error for
  **+17% overall** error. `initial_prompt` is previous-transcript conditioning, not a
  vocabulary channel.

## How it works

```
pw-record ──raw s16──┬──► level meter ──► overlay (layer-shell pill)
                     └──► WAV ──► whisper-server (resident) ──► tidy
                                                                  │
                                      optional local LLM ◄────────┤
                                                                  ▼
                                                      wtype ──► focused window
```

One capture feeds both the meter and the recogniser. The daemon holds the model; the
CLI is a thin client over a unix socket.

## Design notes

Things that are the way they are on purpose:

- **`wtype`, not `ydotool`.** `wtype` uploads its own keymap, so it is immune to your
  keyboard layout. `ydotool` emits raw US scancodes, which on a German QWERTZ layout
  swaps y/z and mangles every punctuation mark — fatal when dictating into a terminal.
  It also needs no `uinput` access and no group membership.
- **The overlay never takes keyboard focus** (`KeyboardMode.NONE`). If it could, the
  synthesised keystrokes would land in the overlay instead of your editor.
- **`libgtk4-layer-shell` is loaded before GTK**, by hand. It interposes on
  `wl_display_connect`, so load order decides whether you get a layer surface or an
  ordinary window that steals focus.
- **`max_duration_secs` is a hard stop.** A missed toggle otherwise becomes a
  multi-minute recording of near-silence, which recognisers hallucinate over
  cheerfully — `[BLANK_AUDIO]`, or "Thank you. Thank you." Those are filtered out.
- **Double-tap, not hold-to-talk.** niri fires keybinds on press only; there is no
  release event available to config, so hold-to-talk is not possible through a keybind
  at all. Reading evdev directly sidesteps the compositor entirely and gives a trigger
  that works everywhere. If you do bind a compositor key instead, use a single
  modifier: holding a chord while text is being typed combines the held modifier with
  every character and fires compositor shortcuts instead of inserting text.

## Limitations

- **Typing into whatever has focus is inherent to the approach.** Focus is not always
  the window you think it is. `utter` never synthesises Return or any control key —
  only the text — but point it somewhere deliberate.
- GNOME and KDE do not implement the virtual-keyboard protocol; use
  `mode = "clipboard"` there.
- `pw-record --target` accepts a PipeWire `node.name` or a numeric serial, and nothing
  else. The PulseAudio-style names from `pactl` work only when they happen to match.
  Run `utter sources`.
- Tested on exactly one machine, one compositor, one GPU.
- The double-tap trigger's logic is unit-tested, and the keyboards are confirmed
  readable, but a uinput device cannot be used to test it end-to-end (uinput nodes get
  no seat ACL), so the final link — a human actually tapping space twice — is verified
  by `utter keys --watch` rather than automatically.
- `live_text` re-recognises the whole utterance on each pass. Fine at conversational
  lengths, wasteful for very long ones. Off by default.

## License

MIT.

---

## ⚠️ Made by a clanka

Every line of this repository — the code, the config, the commit messages, and this
README — was written by Claude, an AI, working from a human's instructions on a single
Arch Linux machine.

What that means for you in practice:

- The measurements above are real, taken on real hardware, and reproducible. They are
  not estimates.
- The pipeline has been tested end-to-end: capture, recognition, cleanup, tray,
  overlay, and keystroke injection all verified working.
- But it has been tested on **one** machine, by a process with no stake in your setup.
  There are no unit tests. Nobody has run this on Sway, Hyprland, an NVIDIA GPU, or a
  non-German keyboard layout.
- This program synthesises keystrokes into your focused window. Read `inject.py`
  before you trust it with that. It is about a hundred lines and deliberately boring.

Review it like you would review code from a stranger who is very fast, very literal,
and occasionally confident about things it has not checked.
