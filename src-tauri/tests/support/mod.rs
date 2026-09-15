//! 集成测试共用构件。
//!
//! 放在 `tests/support/` 子目录里，Cargo 不会把它当独立测试二进制编译，
//! 各测试文件用 `mod support;` 引入即可。
//!
//! 三块：
//! - [`signal`]：合成测试信号（类语音 / 静音）。
//! - [`source`]：把一段样本按真实帧率推进真实 [`CapturePipeline`] 的
//!   [`LoopbackSource`] —— 假源走真管线，重采样与分帧都不是模拟的。
//! - [`fake_sidecar`]：真的 WS 服务端（握手 + 收帧），把「线上到底收到了什么」
//!   变成可断言的记录，而不是靠快照里的计数猜。
//! - [`real_sidecar`]：起真 uvicorn + 真 ASGI 路由的对端（`serve_audio_e2e.py`）。

// 工具箱：不是每样都被当前测试用到（例如 `with_repeat` 是给慢启动的 E2E 留的）。
// 各测试文件各自 `mod support;`，未用的项会在每个二进制里各报一次，纯噪声。
#![allow(dead_code)]

pub mod fake_sidecar;
pub mod real_sidecar;
pub mod signal;
pub mod source;
