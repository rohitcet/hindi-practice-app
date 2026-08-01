"""
Local-only utility: concatenate a collection's five per-text listening MP3s
(<year>_text1_audio.mp3 ... <year>_text5_audio.mp3) into one combined
<year>_listening_audio.mp3, with a short silence gap between texts — matching
the real PSLE listening exam format (all 5 texts played as one recording).

Uses ffmpeg's concat demuxer directly via subprocess (not pydub's
AudioSegment.from_file), since pydub calls ffprobe to inspect MP3s before
decoding and this machine only has the ffmpeg binary bundled by imageio-ffmpeg
(see text_to_listening_audio.py's docstring for the same constraint on the
synthesis side).

Usage:
    python local_tools/concat_listening_audio.py 2021 2022 2023 2024
"""
import argparse
import os
import subprocess
import sys
import tempfile

import imageio_ffmpeg

GAP_SECONDS = 2.0


def build_silence_clip(ffmpeg_exe, path, seconds):
    subprocess.run(
        [ffmpeg_exe, "-y", "-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono",
         "-t", str(seconds), "-c:a", "libmp3lame", "-b:a", "128k", path],
        check=True, capture_output=True,
    )


def concat_year(ffmpeg_exe, year, silence_path, base_dir):
    text_files = [os.path.join(base_dir, f"{year}_text{i}_audio.mp3") for i in range(1, 6)]
    missing = [f for f in text_files if not os.path.isfile(f)]
    if missing:
        print(f"  Skipping {year}: missing {', '.join(os.path.basename(m) for m in missing)}",
              file=sys.stderr)
        return False

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as list_file:
        for i, f in enumerate(text_files):
            list_file.write(f"file '{f}'\n")
            if i < len(text_files) - 1:
                list_file.write(f"file '{silence_path}'\n")
        list_path = list_file.name

    output_path = os.path.join(base_dir, f"{year}_listening_audio.mp3")
    try:
        subprocess.run(
            [ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c:a", "libmp3lame", "-b:a", "128k", output_path],
            check=True, capture_output=True,
        )
    finally:
        os.unlink(list_path)

    print(f"  Wrote {os.path.basename(output_path)}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("years", nargs="+", help="Collection years to concatenate, e.g. 2021 2022")
    args = parser.parse_args()

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    base_dir = os.path.dirname(os.path.abspath(__file__)) + "/.."
    base_dir = os.path.normpath(base_dir)

    with tempfile.TemporaryDirectory() as tmp_dir:
        silence_path = os.path.join(tmp_dir, "silence.mp3")
        build_silence_clip(ffmpeg_exe, silence_path, GAP_SECONDS)

        for year in args.years:
            print(f"=== {year} ===")
            concat_year(ffmpeg_exe, year, silence_path, base_dir)


if __name__ == "__main__":
    main()
