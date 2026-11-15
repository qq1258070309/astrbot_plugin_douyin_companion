"""视频理解证据链。

借鉴 kdcadmin/dsh_douyin_tools 的 ``video-understanding-lab``
（doctor / prepare / asr / record / replay 五个阶段），以纯 Python 重实现：
下载 → 均匀抽帧 → 可选 ASR 转写 → 结构化证据 JSON 落盘 → 可回放。

设计原则沿袭上游：
  * 抽不到 / 依赖缺失就如实返回空 + notes，**绝不编数据**；
  * 所有外部依赖按需导入，缺失只降级该阶段，不影响插件加载；
  * 证据文件只进插件数据目录，不进代码仓库。
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any

from .media import MediaService


def doctor() -> dict[str, Any]:
    """依赖可用性矩阵（不联网、不启动浏览器）。"""
    binaries: dict[str, str | None] = {}
    for name in ("ffmpeg", "ffprobe"):
        from shutil import which
        binaries[name] = which(name)
    modules = {
        name: importlib.util.find_spec(name) is not None
        for name in ("aiohttp", "PIL", "numpy", "faster_whisper", "httpx")
    }
    return {
        "binaries": binaries,
        "modules": modules,
        "asr_available": bool(binaries["ffmpeg"] and modules["faster_whisper"]),
        "frames_available": bool(binaries["ffmpeg"] and binaries["ffprobe"]),
        "automatic_model_backend": False,  # 帧/证据交给外部视觉模型，插件不自带模型推理
    }


class UnderstandingService:
    def __init__(self, *, media: MediaService, data_dir: Path,
                 enable_asr: bool = False, asr_model: str = "base"):
        self.media = media
        self.data_dir = Path(data_dir)
        self.evidence_dir = self.data_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.enable_asr = bool(enable_asr)
        self.asr_model = asr_model or "base"

    async def transcribe(self, video_path: str) -> dict[str, Any]:
        """可选阶段：faster_whisper 本地转写；缺依赖时明确返回 skipped。"""
        if not self.enable_asr:
            return {"status": "disabled", "text": None, "notes": ["enable_asr=false"]}
        if importlib.util.find_spec("faster_whisper") is None:
            return {"status": "skipped", "text": None,
                    "notes": ["faster_whisper 未安装；安装后再开启 enable_asr"]}
        try:
            from faster_whisper import WhisperModel  # type: ignore
            model = WhisperModel(self.asr_model, device="cpu", compute_type="int8")
            segments, info = model.transcribe(video_path, beam_size=5)
            text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
            return {"status": "ok", "text": text[:8000], "language": getattr(info, "language", None),
                    "notes": []}
        except Exception as error:  # 转写失败不能影响抽帧证据
            return {"status": "failed", "text": None,
                    "notes": [f"ASR 失败：{type(error).__name__}"]}

    async def understand(self, aweme_id: str, frame_count: int = 8) -> dict[str, Any]:
        """完整链路：下载 → 抽帧 → 可选 ASR → 证据 JSON。"""
        notes: list[str] = []
        state = doctor()
        if not state["frames_available"]:
            notes.append("ffmpeg/ffprobe 不可用，无法抽帧")
        frames: list[str] = []
        video_path = ""
        meta: dict[str, Any] = {}
        if state["frames_available"]:
            try:
                result = await self.media.frames(aweme_id, frame_count)
                frames = result.get("frames", [])
                video_path = result.get("path", "")
                meta = {k: result.get(k) for k in ("desc", "author", "duration")}
                if not frames:
                    notes.append("抽帧返回 0 张（直链可能失效或视频过短）")
            except Exception as error:
                notes.append(f"抽帧失败：{type(error).__name__}: {error}")
        transcript = await self.transcribe(video_path) if video_path else {
            "status": "skipped", "text": None, "notes": ["无成片可转写"]
        }
        notes.extend(transcript.get("notes", []))

        evidence = {
            "schema": "douyin_companion.evidence.v1",
            "aweme_id": aweme_id,
            "created_at": time.time(),
            "meta": meta,
            "video_path": video_path or None,
            "frames": frames,
            "frame_count": len(frames),
            "transcript": {"status": transcript.get("status"), "text": transcript.get("text"),
                           "language": transcript.get("language")},
            "doctor": state,
            "notes": notes,
            "hint": "frames[] 请交给支持视觉的模型解读；本插件不自带视频理解模型。",
        }
        out = self.evidence_dir / f"{aweme_id}.evidence.json"
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence["evidence_path"] = str(out)
        return evidence

    def replay(self, aweme_id: str) -> dict[str, Any] | None:
        """回放已落盘的证据（不联网）。"""
        path = self.evidence_dir / f"{aweme_id}.evidence.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
