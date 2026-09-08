#!/usr/bin/env python3
"""Read-only MP4 checks; all generated evidence is written to the task verify folder.

Usage:
  python3 scripts/verify_media.py ./output --report-dir ./work/verify
  python3 scripts/verify_media.py /absolute/path/clip.mp4 --report-dir ./work/verify

Exit codes: 0 = all checks passed, 1 = at least one failure, 2 = warnings only.
This technical check does not establish editorial accuracy or the absence of
audible clicks: peak measurements identify clipping risk and require listening.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys


TASK_DIR = Path(__file__).resolve().parent
WORKSPACE = Path.cwd()
VERIFY_DIR = WORKSPACE / "tmp" / "highlight-verification"
REPORT = VERIFY_DIR / "verification.json"
EXPECTED = {"width":1080,"height":1920,"fps":25.0}


def command(args: list[str], timeout: float) -> dict:
    try:
        process = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=timeout, check=False,
        )
        return {
            "command": args,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        def decode(value):
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")

        return {
            "command": args, "returncode": None, "timed_out": True,
            "stdout": decode(error.stdout), "stderr": decode(error.stderr),
        }
    except OSError as error:
        return {
            "command": args, "returncode": None, "timed_out": False,
            "stdout": "", "stderr": str(error),
        }


def numeric(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def rate(value) -> float | None:
    try:
        return numeric(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None


def stream_duration(stream: dict) -> tuple[float | None, str | None]:
    duration = numeric(stream.get("duration"))
    if duration is not None:
        return duration, "stream.duration"
    ticks = numeric(stream.get("duration_ts"))
    base = rate(stream.get("time_base"))
    if ticks is not None and base is not None:
        return ticks * base, "stream.duration_ts * time_base"
    return None, None


def add_check(result: dict, name: str, status: str, details: dict | str):
    result["checks"].append({"name": name, "status": status, "details": details})


def final_status(checks: list[dict]) -> str:
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warning" in statuses:
        return "warning"
    return "pass"


def evidence_log(run: dict, stem: Path) -> dict:
    # Full diagnostics remain available even when the JSON excerpt is truncated.
    log_path = stem.with_suffix(".log")
    log_path.write_text(run["stderr"], encoding="utf-8")
    return {
        "command": run["command"], "returncode": run["returncode"],
        "timed_out": run["timed_out"], "log_path": str(log_path),
        "stderr_excerpt": run["stderr"][-6000:],
    }


def verify(path: Path, ffmpeg: str, ffprobe: str, timeout_override: float | None) -> dict:
    safe_stem = re.sub(r"[^\w.-]", "_", path.stem)[:100]
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:8]
    evidence = VERIFY_DIR / f"{safe_stem}-{digest}"
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"file": str(path), "checks": [], "frames": [], "evidence_dir": str(evidence)}

    probe = command([
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ], timeout_override or 60)
    (evidence / "ffprobe.json").write_text(probe["stdout"], encoding="utf-8")
    if probe["returncode"] != 0:
        add_check(result, "ffprobe", "fail", evidence_log(probe, evidence / "ffprobe"))
        result["status"] = final_status(result["checks"])
        return result
    try:
        metadata = json.loads(probe["stdout"])
    except (ValueError, TypeError) as error:
        add_check(result, "ffprobe", "fail", f"Invalid ffprobe JSON: {error}")
        result["status"] = "fail"
        return result

    streams = metadata.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    add_check(result, "audio_video_streams_present", "pass" if videos and audios else "fail", {
        "video_stream_count": len(videos), "audio_stream_count": len(audios),
    })
    if len(videos) > 1 or len(audios) > 1:
        add_check(result, "multiple_streams", "warning", "Technical checks and snapshots use the first video/audio stream; review additional tracks.")

    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    video_map = f"0:{video['index']}" if "index" in video else "0:v:0"
    audio_map = f"0:{audio['index']}" if "index" in audio else "0:a:0"
    video_duration, video_duration_source = stream_duration(video)
    audio_duration, audio_duration_source = stream_duration(audio)
    container_duration = numeric(metadata.get("format", {}).get("duration"))
    result["metadata"] = {
        "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name"),
        "width": video.get("width"), "height": video.get("height"),
        "pixel_format": video.get("pix_fmt"), "avg_frame_rate": video.get("avg_frame_rate"),
        "r_frame_rate": video.get("r_frame_rate"), "video_duration_seconds": video_duration,
        "audio_duration_seconds": audio_duration, "container_duration_seconds": container_duration,
        "video_duration_source": video_duration_source, "audio_duration_source": audio_duration_source,
        "audio_sample_rate": audio.get("sample_rate"), "audio_channels": audio.get("channels"),
        "video_start_time": numeric(video.get("start_time")),
        "audio_start_time": numeric(audio.get("start_time")),
    }
    add_check(result, "canvas_dimensions", "pass" if video.get("width") == EXPECTED["width"] and video.get("height") == EXPECTED["height"] else "fail", {
        "actual": [video.get("width"), video.get("height")], "expected": [EXPECTED["width"], EXPECTED["height"]],
    })
    avg_fps = rate(video.get("avg_frame_rate"))
    nominal_fps = rate(video.get("r_frame_rate"))
    fps_ok = avg_fps is not None and abs(avg_fps - EXPECTED["fps"]) < 0.001
    add_check(result, "frame_rate", "pass" if fps_ok else "fail", {
        "average_fps": avg_fps, "nominal_fps": nominal_fps, "expected_fps": EXPECTED["fps"],
    })
    if fps_ok and nominal_fps is not None and abs(nominal_fps - EXPECTED["fps"]) >= 0.001:
        add_check(result, "frame_rate_consistency", "warning", "Average frame rate matches the requested rate but nominal rate differs; constant frame rate is unconfirmed.")
    add_check(result, "h264_video", "pass" if video.get("codec_name") == "h264" else "fail", video.get("codec_name"))
    add_check(result, "aac_audio", "pass" if audio.get("codec_name") == "aac" else "fail", audio.get("codec_name"))
    delta = abs(video_duration - audio_duration) if video_duration is not None and audio_duration is not None else None
    add_check(result, "audio_video_duration_difference", "pass" if delta is not None and delta < 0.15 else "fail", {
        "actual_difference_seconds": delta, "required_strictly_less_than_seconds": 0.15,
        "note": "A container duration is not substituted for a missing stream duration.",
    })
    v_start = numeric(video.get("start_time"))
    a_start = numeric(audio.get("start_time"))
    if v_start is not None and a_start is not None and abs(v_start - a_start) >= 0.15:
        add_check(result, "audio_video_start_difference", "warning", {"difference_seconds": abs(v_start - a_start)})

    duration = video_duration if video_duration is not None else container_duration
    timeout = timeout_override or max(180, (duration or 300) * 4 + 120)
    decode = command([
        ffmpeg, "-hide_banner", "-nostdin", "-v", "warning", "-xerror",
        "-i", str(path), "-map", video_map, "-map", audio_map, "-f", "null", "-",
    ], timeout)
    decode_status = "fail" if decode["returncode"] != 0 else ("warning" if decode["stderr"].strip() else "pass")
    add_check(result, "full_audio_video_decode", decode_status, evidence_log(decode, evidence / "decode"))

    if audios:
        volume = command([
            ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-map", audio_map,
            "-vn", "-sn", "-dn", "-af", "volumedetect", "-f", "null", "-",
        ], timeout)
        volume_details = evidence_log(volume, evidence / "volume")
        mean_match = re.search(r"mean_volume:\s*([-+\d.]+)\s*dB", volume["stderr"])
        peak_match = re.search(r"max_volume:\s*([-+\d.]+)\s*dB", volume["stderr"])
        mean_db = numeric(mean_match.group(1)) if mean_match else None
        peak_db = numeric(peak_match.group(1)) if peak_match else None
        volume_details.update({
            "mean_volume_dbfs": mean_db, "max_sample_volume_dbfs": peak_db,
            "measurement": "FFmpeg volumedetect (sample peak, 0.1 dB printed precision)",
            "limitation": "Sample peak does not detect all audible artifacts or intersample peaks. Listening review remains necessary.",
        })
        if volume["returncode"] != 0 or peak_db is None:
            volume_status = "fail"
            volume_details["assessment"] = "Audio level measurement failed; clipping risk is unconfirmed."
        elif peak_db >= -0.1:
            volume_status = "warning"
            volume_details["assessment"] = "Peak is at or very near full scale; clipping risk requires review."
        elif peak_db > -1.0:
            volume_status = "warning"
            volume_details["assessment"] = "Less than 1 dB of measured sample headroom; listen and check true peak before publishing."
        elif peak_db < -35:
            volume_status = "warning"
            volume_details["assessment"] = "Very quiet track; confirm that speech is present and intelligible."
        else:
            volume_status = "pass"
            volume_details["assessment"] = "Measured sample peak has at least 1 dB of headroom. This does not certify absence of audible artifacts."
        add_check(result, "audio_sample_peak", volume_status, volume_details)
    else:
        add_check(result, "audio_sample_peak", "fail", "No audio stream available for measurement.")

    if videos and duration is not None and duration > 0:
        positions = [
            ("start_0p5s", min(0.5, max(0.0, duration - 0.04))),
            ("middle", duration / 2),
            ("end_minus_1s", max(0.0, duration - 1.0)),
        ]
        for label, position in positions:
            output = evidence / f"{label}.png"
            output.unlink(missing_ok=True)
            frame = command([
                ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{position:.6f}",
                "-i", str(path), "-map", video_map, "-frames:v", "1", "-an", "-sn", "-dn",
                "-update", "1", "-compression_level", "3", "-y", str(output),
            ], timeout_override or 120)
            successful = frame["returncode"] == 0 and output.is_file() and output.stat().st_size > 0
            result["frames"].append({"label": label, "time_seconds": position, "file": str(output), "created": successful})
            add_check(result, f"frame_{label}", "pass" if successful else "fail", {
                "file": str(output), "time_seconds": position,
                **({} if successful else evidence_log(frame, evidence / label)),
            })
    else:
        add_check(result, "representative_frames", "fail", "Cannot extract the requested positions without a video stream and a valid duration.")
    result["status"] = final_status(result["checks"])
    return result


def main() -> int:
    global VERIFY_DIR, REPORT, EXPECTED
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="MP4 files or directories (direct children only)")
    parser.add_argument("--timeout", type=float, help="Override per-command timeout in seconds; default scales with duration")
    parser.add_argument("--report-dir",type=Path,default=VERIFY_DIR)
    parser.add_argument("--fps",type=float,default=25)
    parser.add_argument("--width",type=int,default=1080)
    parser.add_argument("--height",type=int,default=1920)
    args = parser.parse_args()
    if not args.paths: parser.error("provide at least one MP4 file or directory")
    if args.fps<=0 or args.width<=0 or args.height<=0: parser.error("canvas and fps must be positive")
    VERIFY_DIR=args.report_dir.expanduser().resolve()
    REPORT=VERIFY_DIR / "verification.json"
    EXPECTED={"width":args.width,"height":args.height,"fps":args.fps}
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be positive")
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)
    requested = args.paths
    paths = []
    input_errors = []
    for requested_path in requested:
        path = requested_path.expanduser().resolve()
        if path.is_dir():
            found = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".mp4")
            paths.extend(found)
            if not found:
                input_errors.append(f"No MP4 files in directory: {path}")
        elif path.is_file() and path.suffix.lower() == ".mp4":
            paths.append(path)
        else:
            input_errors.append(f"Not an existing MP4 file or directory: {path}")
    paths = list(dict.fromkeys(paths))
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    for name, executable in [("ffmpeg", ffmpeg), ("ffprobe", ffprobe)]:
        if not executable:
            input_errors.append(f"Required executable is unavailable: {name}")
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_paths": [str(p) for p in requested],
        "scope": "MP4 technical validation only; source media is never modified. No transcript or manifest content is read.",
        "required_format": {"width": EXPECTED["width"], "height": EXPECTED["height"], "fps": EXPECTED["fps"], "video_codec": "h264", "audio_codec": "aac", "duration_difference_strictly_less_than_seconds": 0.15},
        "input_errors": input_errors, "files": [],
    }
    if ffmpeg and ffprobe:
        for path in paths:
            print(f"Checking {path}", flush=True)
            try:
                item = verify(path, ffmpeg, ffprobe, args.timeout)
            except Exception as error:
                item = {"file": str(path), "status": "fail", "checks": [{"name": "unexpected_verification_error", "status": "fail", "details": f"{type(error).__name__}: {error}"}]}
            report["files"].append(item)
            print(f"  {item['status'].upper()}", flush=True)
    overall_checks = [{"status": item["status"]} for item in report["files"]]
    if input_errors or not report["files"]:
        overall_checks.append({"status": "fail"})
    report["status"] = final_status(overall_checks)
    report["summary"] = {status: sum(item["status"] == status for item in report["files"]) for status in ["pass", "warning", "fail"]}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Overall: {report['status'].upper()} | Report: {REPORT}", flush=True)
    return {"pass": 0, "fail": 1, "warning": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
