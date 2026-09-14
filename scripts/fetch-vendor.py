"""Fetch vendor binaries (libsimple + cppjieba dict) — records source & version.

Verified recipe (task-3, 2026-09-14, Windows x64):
- release: wangfenjin/simple v0.7.1 (2026-02-23, Latest)
- asset: libsimple-windows-x64.zip (5,473,005 bytes)
  URL: https://github.com/wangfenjin/simple/releases/download/v0.7.1/libsimple-windows-x64.zip
  sha256: 7f03cc28cf307721f5621b5a52ef3bcb26c5215de012b09900492eb34d5bed0b
- layout inside zip: libsimple-windows-x64/simple.dll + dict/...
- place: simple.dll → sidecar/vendor/libsimple.dll (rename; load by path);
  dict/{jieba.dict.utf8,hmm_model.utf8,user.dict.utf8,idf.utf8,stop_words.utf8}
  → sidecar/vendor/dict/ (idf.utf8 REQUIRED: jieba_query aborts the process
  without it — see docs/compatibility.md).
- dicts are CppJieba dicts (per dict/README.md in the zip); pinyin data
  updated in v0.7.1 (#202).
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
