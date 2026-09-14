"""云端 REST ASR Provider（降级链第 2 级）：OpenAI `/v1/audio/transcriptions` 兼容。

- 只在**有 API Key** 时进链（`fallback.build_default_chain` 负责判定）。
- 上行前把 float32 段转 16-bit 单声道 WAV（标准库 `wave`，无额外依赖）；
  采样率取调用方给的（本项目恒 16kHz）。
- 错误 kind 与 LLM 侧对齐：`provider_timeout` / `provider_error`（含 http/protocol）。
- R8：key 只进 `Authorization` 头；异常文本只带 URL 与状态码，**永不回显 key**。
- R14：零打印，文本只作返回值。
"""

import io
import wave

import httpx

from .provider import (
    ERR_PROVIDER_ERROR,
    ERR_PROVIDER_TIMEOUT,
    ASRError,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"
TIMEOUT_S = 30.0


def pcm_f32_to_wav_bytes(pcm_f32: bytes, sample_rate: int = 16000) -> bytes:
    """float32 LE 单声道 → 16-bit PCM WAV 字节流（标准库，无第三方依赖）。"""
    import numpy as np

    audio = np.frombuffer(pcm_f32, dtype="<f4")
    pcm16 = np.clip(audio * 32768.0, -32768.0, 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


class CloudRestProvider:
    """OpenAI 兼容的云端整段转写。"""

    name = "cloud-rest"

    def __init__(self, api_key: str, base_url: str | None = None,
                 model: str | None = None, language: str | None = None,
                 timeout_s: float = TIMEOUT_S, client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ValueError("cloud-rest 需要 API Key")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.language = language
        self.timeout_s = timeout_s
        self._client = client

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def transcribe(self, pcm_f32: bytes, sample_rate: int = 16000,
                         path: str | None = None) -> str:
        wav = pcm_f32_to_wav_bytes(pcm_f32, sample_rate)
        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s))
        try:
            resp = await client.post(
                self.base_url + "/audio/transcriptions",
                headers=self._headers(),
                data=data,
                files={"file": ("segment.wav", wav, "audio/wav")},
            )
            if resp.status_code != 200:
                raise ASRError(ERR_PROVIDER_ERROR, f"cloud asr HTTP {resp.status_code}")
            return (resp.json().get("text") or "").strip()
        except ASRError:
            raise
        except httpx.TimeoutException as e:
            raise ASRError(ERR_PROVIDER_TIMEOUT, f"cloud asr 超时（{self.timeout_s}s）") from e
        except httpx.HTTPError as e:
            raise ASRError(ERR_PROVIDER_ERROR, f"cloud asr 连接失败（{type(e).__name__}）") from e
        except (ValueError, KeyError) as e:
            raise ASRError(ERR_PROVIDER_ERROR, f"cloud asr 响应解析失败（{type(e).__name__}）") from e
        finally:
            if self._client is None:
                await client.aclose()

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "language": self.language or "auto"}
