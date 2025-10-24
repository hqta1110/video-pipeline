#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Google-only AI Video Pipeline
--------------------------------
Stages:
    1. script   -> generate script JSON
    2. scenes   -> generate all scenes (requires script.json)
    3. concat   -> combine all videos and audios
Usage:
    python main.py --topic "Đại lễ..." --stage scenes
"""

import os
import json
import argparse
import traceback
import subprocess
from datetime import datetime
from pathlib import Path
from utils import generate_text, tts_speech, VeoClient, AIRequestError, concat_videos


# ==========================================================
# Logging utilities
# ==========================================================
LOG_FILE = "pipeline_log.txt"

def log(msg: str):
    ts = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    text = f"{ts} {msg}"
    print(text)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


# ==========================================================
# Initialization
# ==========================================================
def init_environment():
    try:
        with open("env.json") as f:
            CFG = json.load(f)
        API_KEY = CFG["GOOGLE_API_KEY"]
    except Exception as e:
        log("❌ Failed to load config.json")
        log(traceback.format_exc())
        raise SystemExit(1)

    PROJECT_DIR = Path(__file__).resolve().parent
    OUTPUT_DIR = PROJECT_DIR / "outputs"
    (OUTPUT_DIR / "audio").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "video").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "frames").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "scripts").mkdir(parents=True, exist_ok=True)
    return API_KEY, OUTPUT_DIR


# ==========================================================
# Step 1 — Generate Full Script
# ==========================================================
def generate_full_script(topic: str, api_key: str, out_path: Path, skip_search: bool = False):
    """
    Phase 1: Perform factual web search about the topic
    Phase 2: Generate structured 10-scene script based on gathered info
    """
    try:
        search_prompt = open("prompts/search_prompt.txt", encoding="utf-8").read().replace("{topic}", topic)

        # ---------- Phase 1: Web Search ----------
        log(f"🌐 [Phase 1] Searching the web for factual context about '{topic}' ...")
        if not skip_search:
            search_results = generate_text(
                search_prompt,
                api_key=api_key,
                model="gemini-2.5-flash",
                web_search=True,
                temperature=0.0
            )
            search_file = out_path.parent / "search_context.txt"
            with open(search_file, "w", encoding="utf-8") as f:
                f.write(search_results)
            log(f"✅ [Phase 1] Web search results saved to {search_file}")
        search_results = open(out_path.parent / "search_context.txt", encoding="utf-8").read()

        # ---------- Phase 2: Script Generation ----------
        log(f"🧠 [Phase 2] Generating structured script based on search context...")
        compose_prompt = open("prompts/compose_prompt.txt", encoding="utf-8").read()
        final_prompt = compose_prompt.replace("{topic}", topic).replace("{context}", search_results)
        prompt = (
            final_prompt
            + "\n\nHere is the factual context gathered from the web:\n"
            + search_results[:4000]  # limit to avoid token overflow
        )

        text = generate_text(prompt, api_key=api_key, model="gemini-2.5-flash", max_tokens=10000)
        
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Xóa phần mở đầu như ```json hoặc ```
            cleaned = cleaned.split("```")[1]
            # Nếu có từ 'json' ở đầu dòng, cắt bỏ
            cleaned = cleaned.replace("json", "", 1).strip(" \n")
            # Xóa phần đóng ```
            if "```" in cleaned:
                cleaned = cleaned.split("```")[0].strip()
        data = json.loads(cleaned)

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"✅ [Phase 2] Script generated and saved to {out_path}")

    except AIRequestError as e:
        log(f"❌ API error during script generation: {e}")
        log(traceback.format_exc())
    except json.JSONDecodeError:
        log("❌ Failed to parse Gemini response as JSON (phase 2).")
        log(f"Raw output: {text[:400]}")
    except Exception:
        log("❌ Unexpected error in generate_full_script().")
        log(traceback.format_exc())


# ==========================================================
# Utility — Extract Last Frame
# ==========================================================
def extract_last_frame(video_path: str, frame_path: str):
    try:
        cmd = [
            "ffmpeg", "-sseof", "-1",
            "-i", str(video_path),
            "-update", "1",
            "-q:v", "2",
            str(frame_path), "-y"
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError:
        log(f"⚠️ FFmpeg failed to extract last frame for {video_path}")
    except Exception:
        log(traceback.format_exc())


# ==========================================================
# Step 2 — Generate One Scene
# ==========================================================
def process_scene(scene, prev_scene, api_key, veo, out_dir: Path):
    scene_id = scene["scene_id"]
    ssml = scene["ssml"]
    visual_desc = scene["visual_desc"]
    transition_hint = scene.get("transition_hint", "")

    audio_path = out_dir / "audio" / f"scene_{scene_id:02d}.mp3"
    video_path = out_dir / "video" / f"scene_{scene_id:02d}.mp4"
    frame_path = out_dir / "frames" / f"scene_{scene_id:02d}_last.png"

    log(f"\n🎬 Scene {scene_id} started")

    try:
        # Skip if both audio & video exist
        if audio_path.exists() and video_path.exists():
            log(f"↪️ Scene {scene_id} already exists. Skipping generation.")
            return {"audio": audio_path, "video": video_path, "frame": frame_path}

        # ---- Step 1: TTS ----
        log(f"[TTS] Scene {scene_id}")
        tts_speech(ssml, api_key=api_key, out_path=str(audio_path))
        log(f"[TTS] Done: {audio_path}")

        # ---- Step 2: Veo ----
        if scene_id == 1:
            intro_prompt = """Generate a cinematic 8-second news introduction scene
        featuring a professional Vietnamese female anchor (around 30 years old) in a modern TV newsroom.
        She smiles gently, looks directly at the camera, and greets the audience confidently.
        She wears a red traditional áo dài with subtle golden patterns.
        Lighting: warm and balanced, elegant studio atmosphere.
        Camera: 35mm, eye level, slow push-in motion.
        No on-screen text, only natural movement and environment."""
            prompt = intro_prompt
            image_ref = None
        else:
            base_prompt = open("prompts/scene_prompt.txt", encoding="utf-8").read()
            prompt = base_prompt.format(
                prev_visual=prev_scene["visual_desc"] if prev_scene else "",
                transition_hint=transition_hint,
                main_visual=visual_desc,
            )
            image_ref = (
                out_dir / "frames" / f"scene_{prev_scene['scene_id']:02d}_last.png"
                if prev_scene else None
            )

            image_ref = None

        log(f"[Veo] Generating video for scene {scene_id}")
        veo.generate_video_with_image(
            prompt=prompt,
            image_path=str(image_ref) if image_ref and image_ref.exists() else None,
            duration=8,
            out_path=str(video_path),
        )
        log(f"[Veo] Done: {video_path}")

        # ---- Step 3: Extract Last Frame ----
        extract_last_frame(str(video_path), str(frame_path))
        log(f"[FFMPEG] Extracted last frame for scene {scene_id}")

    except Exception as e:
        log(f"❌ Error in scene {scene_id}: {e}")
        log(traceback.format_exc())
        return None

    return {"audio": audio_path, "video": video_path, "frame": frame_path}


# ==========================================================
# Step 3 — Generate All Scenes
# ==========================================================
def generate_scenes(api_key: str, out_dir: Path, script_path: Path):
    if not script_path.exists():
        log(f"❌ Missing script file: {script_path}")
        return

    with open(script_path, encoding="utf-8") as f:
        scenes = json.load(f)

    veo = VeoClient(api_key=api_key)
    generated = []
    prev_scene = None

    for scene in scenes[:2]:
        result = process_scene(scene, prev_scene, api_key, veo, out_dir)
        if result:
            generated.append(result)
            prev_scene = scene
        else:
            log(f"⚠️ Scene {scene['scene_id']} skipped due to errors.")

    if not generated:
        log("❌ No scenes successfully generated.")
    else:
        log(f"✅ Completed {len(generated)} scenes.")


# ==========================================================
# Step 4 — Concatenate Final Video
# ==========================================================
def concat_final(out_dir: Path):
    audio_dir = out_dir / "audio"
    video_dir = out_dir / "video"
    final_path = out_dir / "final_video.mp4"

    videos = sorted(video_dir.glob("scene_*.mp4"))
    audios = sorted(audio_dir.glob("scene_*.mp3"))
    if not videos or not audios:
        log("❌ Missing scene outputs. Cannot concatenate.")
        return

    try:
        concat_videos([str(v) for v in videos], [str(a) for a in audios], str(final_path))
        log(f"✅ Final video created: {final_path}")
    except Exception:
        log("❌ Error while concatenating final video.")
        log(traceback.format_exc())


# ==========================================================
# Main
# ==========================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google-only AI video pipeline")
    parser.add_argument("--topic", type=str, required=True, help="Topic for the video")
    parser.add_argument("--stage", type=str, choices=["script", "scenes", "concat", "all"], default="all")
    args = parser.parse_args()

    API_KEY, OUTPUT_DIR = init_environment()
    script_path = OUTPUT_DIR / "scripts" / "script.json"

    if args.stage in ["script", "all"]:
        generate_full_script(args.topic, API_KEY, script_path, skip_search=True)

    if args.stage in ["scenes", "all"]:
        generate_scenes(API_KEY, OUTPUT_DIR, script_path)

    if args.stage in ["concat", "all"]:
        concat_final(OUTPUT_DIR)

    log(f"🏁 Pipeline finished with stage: {args.stage}")
