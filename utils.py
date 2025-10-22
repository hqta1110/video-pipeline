#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_api_utils.py — Helpers for AI Thực Chiến endpoints (Google-only models)
Updated to support Veo 3 video generation with optional image input.
"""

import os
import time
import json
import base64
from typing import Any, Dict, List, Optional

import requests

OPENAI_BASE = "https://api.thucchien.ai/v1"
TTS_BASE = "https://api.thucchien.ai"
GEMINI_V1BETA_BASE = "https://api.thucchien.ai/gemini/v1beta"
GEMINI_DOWNLOAD_BASE = "https://api.thucchien.ai/gemini/download"

DEFAULT_TIMEOUT = 120
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0


class AIRequestError(Exception):
    pass


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str],
               timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    backoff = DEFAULT_BACKOFF
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        print("=== RAW ERROR RESPONSE ===")
        print(resp.text)
        raise


def _get_json(url: str, headers: Dict[str, str], timeout: int = DEFAULT_TIMEOUT,
              retries: int = DEFAULT_RETRIES) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    backoff = DEFAULT_BACKOFF
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
            else:
                raise AIRequestError(f"GET {url} failed after {retries} attempts: {e}")
    raise AIRequestError(f"GET {url} failed: {last_err}")


def generate_text(prompt: str, api_key: str, model: str = "gemini-2.5-pro",
                  temperature: float = 0.4, max_tokens: int = 2048,
                  system_prompt: Optional[str] = None,
                  messages: Optional[List[Dict[str, str]]] = None,
                  web_search: bool = False) -> str:
    """
    Generate text using the Gemini API via AI Thực Chiến gateway.
    Supports optional web search augmentation.
    """
    url = f"{OPENAI_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Prepare conversation messages
    if messages is None:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

    # Build payload
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # Enable web search if requested
    if web_search:
        payload["web_search_options"] = {"search_context_size": "large"}

    # Send request
    data = _post_json(url, payload, headers)

    # Extract response
    try:
        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("parts", [{}])[0].get("text")
        if not content:
            raise ValueError("Empty content field in response")
        return content.strip()
    except Exception:
        raise AIRequestError(f"Unexpected text response: {json.dumps(data)[:400]}")



def generate_images(prompt: str, api_key: str, model: str = "imagen-4", n: int = 1,
                    size: Optional[str] = None, output_path: str = "") -> List[bytes]:
    url = f"{OPENAI_BASE}/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"model": model, "prompt": prompt, "n": n}
    if size:
        payload["size"] = size
    data = _post_json(url, payload, headers)

    try:
        image = [base64.b64decode(item["b64_json"]) for item in data.get("data", []) if item.get("b64_json")]
        with open(output_path, "wb") as f:
            f.write(image[0])
        return image
    except Exception:
        raise AIRequestError(f"Unexpected image response: {json.dumps(data)[:400]}")


def tts_speech(input_text_or_ssml: str, api_key: str,
               model: str = "gemini-2.5-flash-preview-tts",
               voice: str = "Zephyr", out_path: str = "speech.mp3") -> str:
    url = f"{TTS_BASE}/audio/speech"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": input_text_or_ssml,
        "voice": voice,
    }

    with requests.post(url, headers=headers, json=payload, stream=True, timeout=DEFAULT_TIMEOUT) as resp:
        resp.raise_for_status()
        with open(out_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return out_path


class VeoClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        self.base = GEMINI_V1BETA_BASE.rstrip('/')
        self.download_base = GEMINI_DOWNLOAD_BASE.rstrip('/')

    def _start_job(self, payload: dict) -> str:
        """Start a long-running video generation job."""
        url = f"{self.base}/models/veo-3.0-generate-001:predictLongRunning"
        print("=== DEBUG PAYLOAD SENT TO API ===")
        import json
        print(json.dumps(payload, indent=2))  # in ra gọn gàng, tránh dài quá
        data = _post_json(url, payload, self.headers)
        name = data.get("name")
        if not name:
            raise AIRequestError(f"No operation name in response: {json.dumps(data)[:400]}")
        print(f"🎬 Started Veo job: {name}")
        return name

    def _poll_until_done(self, operation_name: str, timeout_sec: int = 600) -> str:
        """Poll until video generation completes, then return download URI."""
        op_url = f"{self.base}/{operation_name}"
        start = time.time()
        while True:
            if time.time() - start > timeout_sec:
                raise AIRequestError("⏱️ Veo operation timed out")

            data = _get_json(op_url, self.headers)
            if data.get("done"):
                try:
                    uri = data["response"]["generateVideoResponse"]["generatedSamples"][0]["video"]["uri"]
                    print("✅ Video generation done.")
                    return uri
                except Exception:
                    raise AIRequestError(f"Missing video URI: {json.dumps(data)[:400]}")
            print("⌛ Waiting for video generation to complete...")
            time.sleep(5)

    def _download(self, google_uri: str, out_path: str = "generated_video.mp4") -> str:
        """Download the video file from a Google-provided URI."""
        if google_uri.startswith("https://generativelanguage.googleapis.com/"):
            relative = google_uri.replace("https://generativelanguage.googleapis.com/", "")
        else:
            relative = google_uri
        url = f"{self.download_base}/{relative}"
        with requests.get(url, headers=self.headers, stream=True, timeout=DEFAULT_TIMEOUT) as resp:
            resp.raise_for_status()
            with open(out_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return out_path

    def generate_video_with_image(self, prompt: str, 
                                  out_path: str, image_path: Optional[str]= None, duration: int = 8):
        """High-level function: send job → poll → download result."""
        if not prompt.strip():
            raise ValueError("Prompt is empty!")

        # ---- Encode image reference if available ----
        image_b64 = None
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("utf-8")

        # ---- Build payload ----
        payload = {
            "instances": [
                {
                    "prompt": prompt,
                    **(
                        {
                            "image": {
                                "bytesBase64Encoded": image_b64,
                                "mimeType": "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
                            }
                        }
                        if image_b64 else {}
                    ),
                    # "parameters": {
                    #     "aspectRatio": "16:9",
                    #     "resolution": "720p",
                    #     "negativePrompt": "blurry, low quality, distorted faces, text overlay",
                    #     "personGeneration": "allow_all"
                    # }
                }
            ]
        }


        # ---- Start job ----
        operation_name = self._start_job(payload)

        # ---- Poll until done ----
        video_uri = self._poll_until_done(operation_name)

        # ---- Download file ----
        result_path = self._download(video_uri, out_path)
        print(f"🎉 Saved video to {result_path}")
        return result_path



import subprocess
from pathlib import Path

def concat_videos(video_list, audio_list, output_path):
    """Merge videos + replace with clean audio track"""
    tmp_file = Path("concat_list.txt")
    with open(tmp_file, "w") as f:
        for v in video_list:
            f.write(f"file '{v}'\n")

    merged = Path("merged_temp.mp4")
    subprocess.run(["ffmpeg", "-f", "concat", "-safe", "0",
                    "-i", str(tmp_file), "-c", "copy", str(merged)],
                   check=True)

    # Merge audio with final video
    audio_concat = Path("concat_audio.txt")
    with open(audio_concat, "w") as f:
        for a in audio_list:
            f.write(f"file '{a}'\n")
    merged_audio = Path("merged_audio.mp3")
    subprocess.run(["ffmpeg", "-f", "concat", "-safe", "0",
                    "-i", str(audio_concat), "-c", "copy", str(merged_audio)],
                   check=True)

    subprocess.run([
        "ffmpeg", "-i", str(merged), "-i", str(merged_audio),
        "-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(output_path)
    ], check=True)

    tmp_file.unlink(missing_ok=True)
    audio_concat.unlink(missing_ok=True)
    merged.unlink(missing_ok=True)
    merged_audio.unlink(missing_ok=True)
