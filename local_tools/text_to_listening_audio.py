"""
Local-only utility: turn a Hindi listening-passage script into a single MP3
with automatic male/female voice switching per speaker, ready to attach via
the admin dashboard's "Listening audio" file input.

Runs entirely on your own machine — never touches Railway, never calls the
production server. Uses two separate services, each needing its own
credential:

1. Anthropic API (ANTHROPIC_API_KEY) — reads your script and splits it into
   {text, voice} segments. Some lines can be explicitly labeled (a speaker
   name or "MALE:"/"FEMALE:" followed by a colon) — those are respected
   as-is. Anything unlabeled is inferred from context: character names,
   pronouns, and Hindi gendered verb conjugations (बोला/गया/कहा = male,
   बोली/गई/कही = female). Unattributed narration defaults to a narrator
   voice.

2. Google Cloud Text-to-Speech (GOOGLE_APPLICATION_CREDENTIALS) — synthesizes
   each segment in the matching voice, then this script concatenates them
   into one MP3 with a short pause between speaker turns.

One-time Google Cloud setup:
    - In the Google Cloud Console, enable the "Cloud Text-to-Speech API"
      for your project.
    - Create a service account, grant it a role that can call the API
      (e.g. "Cloud Text-to-Speech User" or "Editor" on a dedicated project),
      and download its JSON key.
    - Set the environment variable GOOGLE_APPLICATION_CREDENTIALS to the
      path of that JSON key file — the Google client library picks it up
      automatically, no code changes needed.

Usage:
    pip install -r local_tools/requirements.txt
    set ANTHROPIC_API_KEY=sk-ant-...
    set GOOGLE_APPLICATION_CREDENTIALS=C:\\path\\to\\service-account.json

    python local_tools/text_to_listening_audio.py script.txt -o collection15_listening.mp3

Script format example (script.txt) — labels are optional, mix freely:
    रवि: क्या तुम कल पार्क आओगी?
    सीमा को यह सुनकर खुशी हुई और वह मुस्कुराई।
    हाँ, ज़रूर आऊँगी, उसने कहा।
"""
import argparse
import io
import json
import os
import sys

import anthropic
import imageio_ffmpeg
from pydub import AudioSegment

AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

# Google Cloud Hindi Neural2 voices — swap these if you prefer different ones
# (list available voices with: client.list_voices(language_code="hi-IN")).
VOICE_NAMES = {"male": "hi-IN-Neural2-B", "female": "hi-IN-Neural2-A"}

SEGMENT_SYSTEM_PROMPT = """You are preparing a Hindi listening-passage script for text-to-speech \
narration with automatic male/female voice switching per speaker.

The script may mix two styles:
- Explicitly labeled lines (a speaker name or "MALE:"/"FEMALE:" followed by a colon) — respect \
these labels exactly.
- Unlabeled narrative or dialogue — infer the speaker's gender from context: character names, \
pronouns, and Hindi gendered verb conjugations (e.g. "बोला", "गया", "कहा" = male; "बोली", "गई", \
"कही" = female). Narration with no identifiable speaker (scene-setting sentences) has no gender of \
its own — treat it as narrator and default to male unless context clearly indicates otherwise.

Split the script into an ordered list of segments, where each segment is a contiguous span spoken \
by one voice. Preserve the original text exactly — never paraphrase, translate, add, or drop any \
content; only decide where to split and which voice each part belongs to. Do not include any \
speaker label/tag in the segment's text field — those are for your own attribution, not narration.

For each segment, output:
- text: the exact original text for this segment (labels/tags stripped out)
- voice: "male" or "female\""""

SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice": {"type": "string", "enum": ["male", "female"]},
                },
                "required": ["text", "voice"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def segment_script(script_text):
    client = anthropic.Anthropic()
    try:
        with client.messages.stream(
            model="claude-sonnet-5",
            max_tokens=32000,
            output_config={"format": {"type": "json_schema", "schema": SEGMENT_SCHEMA}},
            system=SEGMENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": script_text}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.BadRequestError as e:
        print(f"Error: request rejected by the API — {e.message}", file=sys.stderr)
        sys.exit(1)

    if response.stop_reason == "refusal":
        print("Error: Claude declined to process this content.", file=sys.stderr)
        sys.exit(1)
    if response.stop_reason == "max_tokens":
        print("Error: script too long to fully process — try splitting it into smaller pieces.",
              file=sys.stderr)
        sys.exit(1)

    text = next(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text)["segments"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error: Claude's response wasn't valid — {e}", file=sys.stderr)
        sys.exit(1)


def synthesize_segment(tts_client, text, voice):
    # LINEAR16 (WAV) rather than MP3 — pydub can parse WAV via the stdlib `wave` module with no
    # ffmpeg/ffprobe subprocess needed at all. imageio-ffmpeg only bundles an ffmpeg binary (no
    # ffprobe), and pydub's MP3 loading path requires ffprobe to inspect the file first.
    from google.cloud import texttospeech
    input_text = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code="hi-IN", name=VOICE_NAMES[voice])
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.LINEAR16)
    response = tts_client.synthesize_speech(
        input=input_text, voice=voice_params, audio_config=audio_config)
    return response.audio_content


def wav_bytes_to_segment(wav_bytes):
    import wave
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        params = wf.getparams()
        frames = wf.readframes(params.nframes)
    return AudioSegment(
        data=frames, sample_width=params.sampwidth,
        frame_rate=params.framerate, channels=params.nchannels,
    )


def build_combined_audio(segments):
    from google.cloud import texttospeech
    tts_client = texttospeech.TextToSpeechClient()

    combined = AudioSegment.empty()
    pause = AudioSegment.silent(duration=400)
    for i, seg in enumerate(segments):
        print(f"  Synthesizing segment {i + 1}/{len(segments)} ({seg['voice']})...")
        wav_bytes = synthesize_segment(tts_client, seg["text"], seg["voice"])
        clip = wav_bytes_to_segment(wav_bytes)
        if i > 0:
            combined += pause
        combined += clip
    return combined


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("script_path", help="Path to a text file with the listening-passage script")
    parser.add_argument("-o", "--output", help="Output MP3 path (defaults to <script-stem>.mp3)")
    args = parser.parse_args()

    if not os.path.isfile(args.script_path):
        print(f"Error: file not found — {args.script_path}", file=sys.stderr)
        sys.exit(1)

    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        print("Error: GOOGLE_APPLICATION_CREDENTIALS is not set — see this script's docstring "
              "for one-time Google Cloud setup steps.", file=sys.stderr)
        sys.exit(1)

    with open(args.script_path, encoding="utf-8") as f:
        script_text = f.read().strip()
    if not script_text:
        print("Error: script file is empty.", file=sys.stderr)
        sys.exit(1)

    stem = os.path.splitext(os.path.basename(args.script_path))[0]
    output_path = args.output or f"{stem}.mp3"

    print("Splitting script into voiced segments...")
    segments = segment_script(script_text)
    print(f"  Got {len(segments)} segment(s): "
          f"{sum(1 for s in segments if s['voice'] == 'male')} male, "
          f"{sum(1 for s in segments if s['voice'] == 'female')} female")

    print("Generating audio...")
    combined = build_combined_audio(segments)
    combined.export(output_path, format="mp3", bitrate="128k")

    print(f"\nWrote {output_path} ({len(combined) / 1000:.1f}s)")
    print("Attach this file via the admin dashboard's 'Listening audio' file input.")


if __name__ == "__main__":
    main()
