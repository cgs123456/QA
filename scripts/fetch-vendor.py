"""Fetch vendor binaries (libsimple + cppjieba dict) — records source & version.

This skeleton task does NOT vendor binaries (see .gitignore).
Real fetch (Phase 1a D3) must fill in exact URLs + SHA256 and download:

- libsimple: https://github.com/wangfenjin/simple
  - Windows: vendor/libsimple.dll (~1-2MB)
  - Linux:   vendor/libsimple.so
  - macOS:   vendor/libsimple.dylib
- cppjieba dict (vendor/dict/):
  - jieba.dict.utf8  (main dict, MPSegment)
  - hmm_model.utf8   (HMM model, MixSegment required)
  - user.dict.utf8   (user dict)

After fetch, verify per PRD §3.13:
  1. dict/ three required files exist (PyInstaller silently packs empty dirs)
  2. SELECT jieba_dict('<abs vendor/dict/>') succeeds
  3. smoke test inserts data BEFORE MATCH/rebuild (empty-table test is false-green)
  4. load_extension() + jieba_dict() use ABSOLUTE paths anchored at
     sys._MEIPASS / executable dir (packaged) or project root (dev, env-overridable)

Usage (not yet implemented — placeholder):
    python scripts/fetch-vendor.py --platform windows --out sidecar/vendor
"""

SOURCES = {
    "libsimple": {
        "repo": "https://github.com/wangfenjin/simple",
        "license": "MIT/GPL-3.0 dual (use under MIT, keep copyright notice)",
        "assets": {
            "windows": "vendor/libsimple.dll",
            "linux": "vendor/libsimple.so",
            "macos": "vendor/libsimple.dylib",
        },
    },
    "cppjieba_dict": {
        "files": ["jieba.dict.utf8", "hmm_model.utf8", "user.dict.utf8"],
    },
}


def main() -> None:
    raise NotImplementedError(
        "fetch-vendor not implemented in skeleton task; see docstring for sources. "
        "Phase 1a D3 must implement download + SHA256 verify."
    )


if __name__ == "__main__":
    main()
