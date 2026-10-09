# 第三方组件与归属声明

本项目的网络认证协议实现基于以下开源项目，按其许可证要求在此声明。

---

## campusnet

- 仓库：https://github.com/demo133/campusnet
- 许可证：MIT
- 版权：Copyright (c) 2026 campusnet contributors
- 许可证全文：见本目录下 `LICENSE.campusnet`

### 使用方式

`campusnet/` 目录是该项目的**原始代码副本**，本项目在其之上做了修改。
每处修改都在对应代码的注释里写明了原因，主要如下：

| 文件 | 修改内容 |
|---|---|
| `campusnet/wifi.py` | 新增 `_decode_output()` 自动判定子进程输出编码（UTF-8 优先、GBK 兜底）；`_run()` 不再使用 `text=True`；新增 `permission_denied()` 与 `_warn_elevation_once()`，权限不足时跳过无意义的等待与重试 |
| `campusnet/runner.py` | `ensure_online()` 新增 `portal_url` 参数以支持传入带会话令牌的门户地址；权限受限导致的 Wi-Fi 跳过不再输出误导性日志；`already_online` 与「刚认证成功」分开表述 |
| `campusnet/providers/ruijie.py` | `_fetch_query_string()` 改为「门户/跳转 URL 优先 → 页面抠取 → 自拼兜底」，自拼时 `nasip` 不再被填成门户 URL；新增 `_query_from_url()`、`_query_from_text()`、`_decode_message()` |

### 为什么保留完整源代码

`campusnet/` 是本项目认证逻辑的实际实现（不是可选依赖）。
保留完整源码既是 MIT 许可证的要求，也方便使用者审阅与自行适配其他学校。
